"""PDF 解析 pipeline 编排: VLM 抽取 -> (可选) 文本校验 -> (可选) OCR 可视化."""
import asyncio
import io
import base64
import logging
from typing import TypeVar
from PIL import Image
from pydantic import BaseModel
from ...tasks import TaskStatus, set_phase
from ...utils import exception_detail
from .vlm import ask_vlm
from .text import validate_text
from .ocr import map_ocr


T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


# pipeline 内部 phase 名 (字符串). 路由层 GET /xxx/data/{task_id} / image/{task_id}
# 在自己的 handler 里直接传同样的字符串去 ``get_task``, 不需要再通过 enum.
PHASE_VLM = "vlm"
PHASE_OCR = "ocr"

DEFAULT_VLM_TIMEOUT = 180
DEFAULT_TEXT_TIMEOUT = 60
DEFAULT_OCR_TIMEOUT = 300


def _png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _phase_error(exc: Exception, phase: str, timeout: int) -> object:
    if isinstance(exc, TimeoutError):
        return f"{phase} 超时（>{timeout}s）"
    return exception_detail(exc)


async def pdf_pipeline(
    task_id: str,
    pdf_bytes: bytes,
    schema: type[T],
    *,
    model: str,
    prompt: str | None = None,
    vlm_retry: int = 2,
    text_check: bool = False,
    ocr_image: bool = False,
    mode: str = "auto",
    vlm_timeout: int = DEFAULT_VLM_TIMEOUT,
    text_timeout: int = DEFAULT_TEXT_TIMEOUT,
    ocr_timeout: int = DEFAULT_OCR_TIMEOUT,
    # image mode 渲染参数
    dpi: int = 150,
    jpeg_quality: int = 85,
    max_dim: int = 2048,
    grayscale: bool = False,
    page_start: int = 0,
    page_end: int | None = None,
    **kwargs,
):
    """
    PDF 解析 pipeline:
        1. VLM 抽取 + schema 校验
        2. (可选) 针对 native-PDF 的文本校验
        3. (可选) OCR 文本区域可视化 -> base64 PNG
    """
    set_phase(task_id, PHASE_VLM, status=TaskStatus.processing)
    current_step = "VLM 抽取"
    current_timeout = vlm_timeout
    try:
        data = await asyncio.wait_for(
            ask_vlm(
                pdf_bytes,
                schema,
                model=model,
                prompt=prompt,
                retry=vlm_retry,
                mode=mode,
                dpi=dpi,
                jpeg_quality=jpeg_quality,
                max_dim=max_dim,
                grayscale=grayscale,
                page_start=page_start,
                page_end=page_end,
            ),
            timeout=vlm_timeout,
        )
        if text_check:
            current_step = "文本校验"
            current_timeout = text_timeout
            data = await asyncio.wait_for(
                validate_text(data, pdf_bytes, **kwargs),
                timeout=text_timeout,
            )
        set_phase(task_id, PHASE_VLM, status=TaskStatus.success, result=data)
    except Exception as e:
        logger.exception("PDF VLM phase failed, task_id=%s", task_id)
        set_phase(
            task_id,
            PHASE_VLM,
            status=TaskStatus.error,
            message=_phase_error(e, current_step, current_timeout),
        )
        if ocr_image:
            set_phase(
                task_id,
                PHASE_OCR,
                status=TaskStatus.error,
                message="VLM 阶段失败，OCR 可视化未执行",
            )
        return

    if not ocr_image:
        set_phase(task_id, PHASE_OCR, status=TaskStatus.success, result=[])
        return

    set_phase(task_id, PHASE_OCR, status=TaskStatus.processing)
    try:
        imgs = await asyncio.wait_for(
            map_ocr(data, pdf_bytes, **kwargs),
            timeout=ocr_timeout,
        )
        b64s = [_png_b64(img) for img in imgs]
        set_phase(task_id, PHASE_OCR, status=TaskStatus.success, result=b64s)
    except Exception as e:
        logger.exception("PDF OCR phase failed, task_id=%s", task_id)
        set_phase(task_id, PHASE_OCR, status=TaskStatus.error, message=_phase_error(e, "OCR 可视化", ocr_timeout))
