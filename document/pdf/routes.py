import importlib
import pkgutil

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException

from ..tasks import TaskResult, create_task, get_task
from ._common import PdfExtractionAck, dump_schema_fields, pdf_params
from .pipeline.runner import pdf_pipeline

router = APIRouter()

# 自动发现 clients/ 下所有子包, 触发各自的 support() 声明
from . import clients as _clients_pkg  # noqa: E402

for _importer, _pkg_name, _ispkg in pkgutil.iter_modules(_clients_pkg.__path__):
    if not _pkg_name.startswith("_") and _ispkg:
        importlib.import_module(f".clients.{_pkg_name}", package=__package__)

from .clients import all_labels, get_config  # noqa: E402


def _resolve_client(client: str):
    found = get_config(client)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "pdf_client_not_found",
                "message": f"未知 client: {client!r}",
                "client": client,
                "available_clients": sorted(all_labels()),
            },
        )
    return found


@router.post("", summary="提交 PDF 解析任务 (按 client 参数自动派发)")
async def pdf_create(
    tasks: BackgroundTasks,
    client: str = Form(..., description="客户标识, e.g. retro"),
    params: dict = Depends(pdf_params),
) -> dict:
    schema, schema_label = _resolve_client(client)
    pdf_bytes = params.pop("pdf_bytes")
    file_name = params.pop("file_name", "(未命名)")
    text_check = params["text_check"]
    ocr_image = params["ocr_image"]

    ack = await create_task(tasks, pdf_pipeline, pdf_bytes, schema, **params)
    ack_payload = PdfExtractionAck(
        task_id=ack.task_id,
        file_name=file_name,
        schema_label=schema_label,
        fields=dump_schema_fields(schema),
        text_check=text_check,
        ocr_image=ocr_image,
    ).model_dump()
    task_id = ack_payload.pop("task_id")
    file_name = ack_payload.pop("file_name")
    return {
        "client": client,
        "engine": "pdf",
        "task_id": task_id,
        "file_name": file_name,
        "ack": ack_payload,
    }


@router.get("/data/{task_id}", summary="取 PDF 解析结果")
async def pdf_data(task_id: str) -> TaskResult:
    return await get_task(task_id, "vlm")


@router.get("/image/{task_id}", summary="取 PDF OCR 可视化图")
async def pdf_image(task_id: str) -> TaskResult[list[str]]:
    return await get_task(task_id, "ocr")
