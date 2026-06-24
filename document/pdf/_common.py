import types
from enum import Enum
from typing import Any, Union, get_args, get_origin

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field

from ..settings import DEFAULT_MODEL
from ..tasks import TaskResult, create_task, get_task


async def pdf_params(
    pdf: UploadFile = File(..., description="PDF 文件"),
    model: str = Form(DEFAULT_MODEL, description=f"模型别名 (取自 settings.yaml), 默认 `{DEFAULT_MODEL}`"),
    prompt: str = Form(None, description="提示词, 不传则使用内置默认"),
    vlm_retry: int = Form(2, ge=0, description="VLM 输出 schema 校验的最大重试次数, ≥ 0, 默认 `2`"),
    vlm_timeout: int = Form(180, ge=1, description="VLM 抽取超时秒数, 默认 `180`"),
    text_timeout: int = Form(60, ge=1, description="文本校验超时秒数, 默认 `60`"),
    ocr_timeout: int = Form(300, ge=1, description="OCR 可视化超时秒数, 默认 `300`"),
    text_check: bool = Form(False, description="是否做文本校验 (仅 **native-PDF** 有效), 默认 `False`"),
    ocr_image: bool = Form(False, description="是否绘制 OCR 区域可视化图, 默认 `False` *(略影响性能)*"),
    threshold: int = Form(75, ge=0, le=100, description="OCR / 文本校验模糊匹配阈值, 默认 `75`"),
    exclude: list[str] = Form(None, description="不需要校验的字段路径, 默认 `None`"),
    include: list[str] = Form(None, description="只校验这些字段路径, 默认 `None`"),
    mode: str = Form("auto", description="PDF 处理模式: auto/native/image/markdown, 默认 `auto`"),
) -> dict:
    pdf_bytes = await pdf.read()
    return {
        "pdf_bytes": pdf_bytes,
        "file_name": pdf.filename or "(未命名)",
        "model": model,
        "prompt": prompt,
        "vlm_retry": vlm_retry,
        "vlm_timeout": vlm_timeout,
        "text_timeout": text_timeout,
        "ocr_timeout": ocr_timeout,
        "text_check": text_check,
        "ocr_image": ocr_image,
        "score_cutoff": threshold,
        "exclude": exclude,
        "include": include,
        "mode": mode,
    }


class SchemaFieldPreview(BaseModel):
    path: str = Field(
        ...,
        description="字段路径; 嵌套用 '.', list 用 '[]' 标记. e.g. 'header.documentType' / 'data[].deliveryNote'.",
    )
    type: str = Field(
        ...,
        description=(
            "字段类型可读名. 原子类型用 Python 类名 (str/int/...), 枚举展开成员 "
            "(e.g. 'DocumentType[Belastung|Gutschrift]'), list 内非 BaseModel 用 'list[T]'."
        ),
    )
    description: str | None = Field(
        None,
        description="schema 上 ``Field(description=...)`` 写的说明; 没写为 null.",
    )
    required: bool = Field(..., description="字段是否必填.")


def _unwrap_optional(ann: Any) -> Any:
    origin = get_origin(ann)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(ann) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return ann


def _type_label(ann: Any) -> str:
    if isinstance(ann, type):
        if issubclass(ann, Enum):
            members = "|".join(m.name for m in ann)
            return f"{ann.__name__}[{members}]"
        return ann.__name__
    return str(ann)


def _is_basemodel(x: Any) -> bool:
    return isinstance(x, type) and issubclass(x, BaseModel)


def dump_schema_fields(schema: type[BaseModel], *, _prefix: str = "") -> list[SchemaFieldPreview]:
    out: list[SchemaFieldPreview] = []
    for name, info in schema.model_fields.items():
        path = f"{_prefix}{name}"
        ann = _unwrap_optional(info.annotation)
        origin = get_origin(ann)
        args = get_args(ann)

        if origin is list and args:
            inner = _unwrap_optional(args[0])
            if _is_basemodel(inner):
                out.extend(dump_schema_fields(inner, _prefix=f"{path}[]."))
                continue
            type_name = f"list[{_type_label(inner)}]"
        elif _is_basemodel(ann):
            out.extend(dump_schema_fields(ann, _prefix=f"{path}."))
            continue
        else:
            type_name = _type_label(ann)

        out.append(SchemaFieldPreview(
            path=path,
            type=type_name,
            description=info.description,
            required=info.is_required(),
        ))
    return out


class PdfExtractionAck(BaseModel):
    task_id: str = Field(..., description="任务 id, 用来 GET 轮询拿结果.")
    file_name: str = Field(..., description="客户上传的原始文件名 (可能是 ``'(未命名)'``).")
    schema_label: str = Field(
        ...,
        description="本次抽取目标的可读名 (e.g. ``'Retro-Billing 凭证'``), 给前端 UI 标题用.",
    )
    fields: list[SchemaFieldPreview] = Field(
        ...,
        description="本次抽取目标 schema 拍平后的字段清单, 由 ``dump_schema_fields(schema)`` 生成.",
    )
    text_check: bool = Field(
        ...,
        description="本次任务是否做文本校验 (form 覆盖后的最终值, 跟 ``pdf_params`` 同名 form 字段对齐).",
    )
    ocr_image: bool = Field(
        ...,
        description="本次任务是否生成 OCR 区域可视化图. ``True`` 时 ``GET /{prefix}/image/{task_id}`` 才有内容.",
    )


__all__ = (
    "pdf_params",
    "SchemaFieldPreview",
    "dump_schema_fields",
    "PdfExtractionAck",
    "register_pdf_routes",
)


def register_pdf_routes(
    router: APIRouter,
    *,
    label: str,
    schema: type[BaseModel],
    schema_label: str,
) -> None:
    """给一个 PDF 解析 schema + URL 前缀, 在 ``router`` 上注册 POST + GET×2 三个端点.

    新客户接入: 在 ``pdf/clients/<name>/__init__.py`` 里调一次即可, 不必自己手写三个
    路由函数. 路由命名约定:
        * ``POST  /{label}``                  -> 提交任务, 返 ``PdfExtractionAck``
        * ``GET   /{label}/data/{task_id}``   -> 取 VLM 解析结果 ``TaskResult[schema]``
        * ``GET   /{label}/image/{task_id}``  -> 取 OCR 可视化图 ``TaskResult[list[str]]``

    Args:
        router: 已创建好的 ``APIRouter`` (通常是 ``apdfi.pdf.routes.router``).
        label: URL-safe 标识 (e.g. ``"retro"``), 用作路径前缀 + OpenAPI name 后缀.
        schema: VLM 抽取结果的 pydantic 类型.
        schema_label: 给前端 ACK 里 ``schema_label`` 字段用的可读名称.
    """
    from .pipeline.runner import pdf_pipeline

    @router.post(
        f"/{label}",
        summary=f"提交 {label} PDF 解析任务, 返回 task_id + 抽取字段 preview",
        name=f"{label}_pdf_create",
    )
    async def _create(
        tasks: BackgroundTasks,
        params: dict = Depends(pdf_params),
    ) -> PdfExtractionAck:
        pdf_bytes = params.pop("pdf_bytes")
        file_name = params.pop("file_name", "(未命名)")
        text_check = params["text_check"]
        ocr_image = params["ocr_image"]
        ack = await create_task(tasks, pdf_pipeline, pdf_bytes, schema, **params)
        return PdfExtractionAck(
            task_id=ack.task_id,
            file_name=file_name,
            schema_label=schema_label,
            fields=dump_schema_fields(schema),
            text_check=text_check,
            ocr_image=ocr_image,
        )

    @router.get(
        f"/{label}/data/{{task_id}}",
        summary=f"取 {label} VLM 解析结果",
        name=f"{label}_pdf_data",
    )
    async def _data(task_id: str) -> TaskResult[schema]:
        return await get_task(task_id, "vlm")

    @router.get(
        f"/{label}/image/{{task_id}}",
        summary=f"取 {label} OCR 可视化图",
        name=f"{label}_pdf_image",
    )
    async def _image(task_id: str) -> TaskResult[list[str]]:
        return await get_task(task_id, "ocr")

