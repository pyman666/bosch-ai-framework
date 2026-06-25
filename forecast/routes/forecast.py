"""预测执行路由 — /api/v1/forecast/*"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forecast.database import get_db
from forecast.models.forecast import (
    ForecastAccuracyRequest, ForecastAccuracyResponse,
    ForecastInput, ForecastOutput,
    TrialCalculationRequest, TrialCalculationResponse,
)
from forecast.models.skill import SkillStatus
from forecast.core import skill_manager
from forecast.core.accuracy import compute_accuracy
from forecast.core.executor import execute_skill
from forecast.core.heavy_skill import is_heavy_skill
from infra.task import TaskID, TaskResult, create_task, get_task, set_phase, TaskStatus


class BatchForecastRequest(BaseModel):
    """批量预测请求"""
    skill_id: str = Field(..., description="技能ID")
    inputs: list[ForecastInput] = Field(..., description="输入数据列表")
    max_concurrency: int = Field(default=10, description="最大并发数")


class BatchForecastResponse(BaseModel):
    """批量预测响应"""
    results: list[ForecastOutput]
    success_count: int
    error_count: int
    total_count: int


class AsyncBatchRequest(BaseModel):
    """异步批量预测请求"""
    inputs: list[ForecastInput] = Field(..., description="输入数据列表")
    max_concurrency: int = Field(default=10, description="最大并发数")


router = APIRouter(prefix="/api/v1/forecast", tags=["forecast"])


# ---------------------------------------------------------------------------
# Forecast execution
# ---------------------------------------------------------------------------

@router.post("/run/{skill_id}", response_model=list[ForecastOutput])
async def run_forecast(
    skill_id: str,
    inputs: list[ForecastInput],
    db: Session = Depends(get_db),
):
    """使用已激活的 Skill 执行预测。"""
    skill = skill_manager.get_skill(db, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.status != SkillStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Skill is not active")

    # 预热编译缓存：Python skill 预编译，batch 内复用
    if skill.skill_type.value == "python" and skill.python_code:
        from forecast.core.executor import prepare_python_skill
        prepare_python_skill(skill.python_code)

    return await asyncio.to_thread(_run_forecast_sync, skill, inputs, skill_id)


@router.post("/batch/{skill_id}", response_model=BatchForecastResponse)
async def run_batch_forecast(
    skill_id: str,
    db: Session = Depends(get_db),
    body: BatchForecastRequest | None = None,
    inputs: list[ForecastInput] | None = None,
):
    """批量并行执行预测（支持多种输入格式）。"""
    # 支持两种输入方式：body.inputs 或直接传 inputs
    all_inputs = (body.inputs if body else None) or inputs or []
    if not all_inputs:
        raise HTTPException(status_code=400, detail="No input data provided")

    skill = skill_manager.get_skill(db, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.status != SkillStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Skill is not active")

    max_concurrency = body.max_concurrency if body else 10

    # 预热编译缓存：Python skill 在 batch 开始前预编译，避免首条记录的编译延迟
    if skill.skill_type.value == "python" and skill.python_code:
        from forecast.core.executor import prepare_python_skill
        try:
            prepare_python_skill(skill.python_code)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Skill compilation failed: {e}")

    # 重计算 skill（ARIMA/Holt-Winters 等）强制降低并发，保护服务
    heavy = is_heavy_skill(
        skill.skill_type.value,
        preset_name=skill.preset_name,
        python_code=skill.python_code,
    )
    if heavy:
        max_concurrency = min(max_concurrency, 3)

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_one(inp: ForecastInput) -> ForecastOutput:
        async with semaphore:
            return await asyncio.to_thread(
                _execute_safe,
                skill.skill_type.value,
                skill.dsl_expression,
                skill.python_code,
                skill.preset_name,
                inp,
                skill_id,
                skill.name,
            )

    tasks = [_run_one(inp) for inp in all_inputs]
    results = await asyncio.gather(*tasks)

    success = sum(1 for r in results if not r.metadata.get("error"))
    errors = len(results) - success

    response = BatchForecastResponse(
        results=results,
        success_count=success,
        error_count=errors,
        total_count=len(results),
    )

    # 重计算 skill 提醒前端
    from fastapi.responses import JSONResponse
    if heavy:
        return JSONResponse(
            content=response.model_dump(),
            headers={
                "X-Heavy-Skill": "true",
                "X-Heavy-Skill-Warning": "此 skill 计算密集型（ARIMA/Holt-Winters），"
                "大批量建议使用 /forecast/async-batch 异步端点",
            },
        )
    return response


def _run_forecast_sync(skill, inputs, skill_id):
    """同步执行预测（在 asyncio.to_thread 中运行）。"""
    results = []
    for inp in inputs:
        try:
            output = execute_skill(
                skill_type=skill.skill_type.value,
                dsl_expression=skill.dsl_expression,
                python_code=skill.python_code,
                preset_name=skill.preset_name,
                input_data=inp,
            )
            output.metadata["skill_id"] = skill_id
            output.metadata["skill_name"] = skill.name
        except Exception as e:
            output = ForecastOutput(
                **inp.extra_for_output,
                forecast=[],
                metadata={
                    "skill_id": skill_id,
                    "skill_name": skill.name,
                    "error": str(e),
                },
            )
        results.append(output)
    return results


def _trial_calculation_sync(skill_type, payload):
    """同步执行试算（在 asyncio.to_thread 中运行）。"""
    results = []
    for inp in payload.input_data:
        try:
            output = execute_skill(
                skill_type=skill_type,
                dsl_expression=payload.dsl_expression,
                python_code=payload.python_code,
                preset_name=None,
                input_data=inp,
            )
        except Exception as e:
            output = ForecastOutput(
                **inp.extra_for_output,
                forecast=[],
                metadata={"error": str(e)},
            )
        results.append(output)

    errors = [r.metadata.get("error") for r in results if r.metadata.get("error")]
    return TrialCalculationResponse(
        results=results,
        error="; ".join(errors) if errors else None,
    )


def _execute_safe(
    skill_type, dsl_expr, python_code, preset_name,
    inp, skill_id, skill_name,
) -> ForecastOutput:
    """安全执行单个记录，捕获异常。"""
    try:
        output = execute_skill(
            skill_type=skill_type,
            dsl_expression=dsl_expr,
            python_code=python_code,
            preset_name=preset_name,
            input_data=inp,
        )
        output.metadata["skill_id"] = skill_id
        output.metadata["skill_name"] = skill_name
        return output
    except Exception as e:
        return ForecastOutput(
            **inp.extra_for_output,
            forecast=[],
            metadata={
                "skill_id": skill_id,
                "skill_name": skill_name,
                "error": str(e),
            },
        )


# ---------------------------------------------------------------------------
# Trial calculation
# ---------------------------------------------------------------------------

@router.post("/trial", response_model=TrialCalculationResponse)
async def trial_calculation(payload: TrialCalculationRequest):
    """使用临时公式执行试算（不保存）。"""
    if not payload.dsl_expression and not payload.python_code:
        raise HTTPException(status_code=400, detail="Must provide dsl_expression or python_code")

    skill_type = "dsl" if payload.dsl_expression else "python"

    return await asyncio.to_thread(_trial_calculation_sync, skill_type, payload)


# ---------------------------------------------------------------------------
# Forecast accuracy evaluation
# ---------------------------------------------------------------------------

@router.post("/evaluate", response_model=ForecastAccuracyResponse)
def evaluate_forecast_accuracy(payload: ForecastAccuracyRequest):
    """评估预测准确度，返回 MAE / MAPE / RMSE / sMAPE 指标。

    预测值和实际值按日期对齐后计算，仅统计两者都有的日期。
    """
    metrics = compute_accuracy(payload.forecast, payload.actual)
    return ForecastAccuracyResponse(metrics=metrics)


# ---------------------------------------------------------------------------
# Async batch forecast
# ---------------------------------------------------------------------------

@router.post("/async-batch/{skill_id}", response_model=TaskID)
async def run_async_batch_forecast(
    skill_id: str,
    background_tasks: BackgroundTasks,
    body: AsyncBatchRequest,
    db: Session = Depends(get_db),
):
    """异步批量预测，提交后返回 task_id，通过 GET /tasks/{task_id} 轮询结果."""
    skill = skill_manager.get_skill(db, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.status != SkillStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Skill is not active")

    # 预热编译缓存
    if skill.skill_type.value == "python" and skill.python_code:
        from forecast.core.executor import prepare_python_skill
        try:
            prepare_python_skill(skill.python_code)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Skill compilation failed: {e}")

    # 重计算 skill 强制限制并发
    if is_heavy_skill(skill.skill_type.value, preset_name=skill.preset_name, python_code=skill.python_code):
        body.max_concurrency = min(body.max_concurrency, 3)

    return await create_task(
        background_tasks,
        _execute_async_batch,
        skill_id,
        skill.name,
        skill.skill_type.value,
        skill.dsl_expression,
        skill.python_code,
        skill.preset_name,
        body.inputs,
    )


@router.get("/tasks/{task_id}", response_model=TaskResult)
async def get_async_batch_result(task_id: str):
    """查询异步批量预测任务状态和结果."""
    return await get_task(task_id, "forecast")


def _execute_async_batch(
    task_id: str,
    skill_id: str,
    skill_name: str,
    skill_type: str,
    dsl_expression: str | None,
    python_code: str | None,
    preset_name: str | None,
    inputs: list[ForecastInput],
):
    """后台执行批量预测，通过 set_phase 上报进度和结果."""
    set_phase(task_id, "forecast", status=TaskStatus.processing, progress=f"0/{len(inputs)}")

    results: list[ForecastOutput] = []
    batch_size = 500

    for i in range(0, len(inputs), batch_size):
        batch = inputs[i:i + batch_size]
        for j, inp in enumerate(batch):
            try:
                output = execute_skill(
                    skill_type=skill_type,
                    dsl_expression=dsl_expression,
                    python_code=python_code,
                    preset_name=preset_name,
                    input_data=inp,
                )
                output.metadata["skill_id"] = skill_id
                output.metadata["skill_name"] = skill_name
                results.append(output)
            except Exception as e:
                results.append(ForecastOutput(
                    **inp.extra_for_output,
                    forecast=[],
                    metadata={"skill_id": skill_id, "skill_name": skill_name, "error": str(e)},
                ))

            # 每批更新进度
            completed = min(i + j + 1, len(inputs))
            set_phase(task_id, "forecast", status=TaskStatus.processing, progress=f"{completed}/{len(inputs)}")

    success = sum(1 for r in results if not r.metadata.get("error"))
    error = len(results) - success

    set_phase(
        task_id,
        "forecast",
        status=TaskStatus.success,
        result={
            "results": [r.model_dump() for r in results],
            "success_count": success,
            "error_count": error,
            "total_count": len(results),
        },
    )
