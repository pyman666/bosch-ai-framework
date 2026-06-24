import base64
from enum import Enum
from io import BytesIO
from typing import Any, TypeVar
from concurrent.futures import ThreadPoolExecutor

import fitz  # pymupdf
import pymupdf4llm
from PIL import Image
from pydantic import BaseModel
from fastapi.concurrency import run_in_threadpool

from ...settings import DEFAULT_MODEL
from ...llm import instructor_call


T = TypeVar("T", bound=BaseModel)

_NATIVE_PDF_MODEL_PREFIXES = (
    "claude-",
    "gemini-",
)

_PYMUPDF_LAYOUT_PATCHED = False


class PdfMode(str, Enum):
    """PDF 处理模式。"""
    AUTO = "auto"
    NATIVE = "native"
    IMAGE = "image"
    MARKDOWN = "markdown"

PDF_PROMPT = r"""
你是一名专业的 OCR/VLM 信息抽取助手. 你需要从 PDF 中提取 schema 中给定的字段, 并严格
输出 JSON.

输出要求:
- 只能输出 JSON, 不要输出任何解释 / 说明 / markdown 代码块;
- JSON 中必须包含 schema 里所有字段, 即便对应内容在 PDF 里找不到也要给出明确的空值
  (而不是缺字段);
- 单字段内容跨多行时用 '\n' 连接, 保留原文换行结构.
"""

PDF_IMAGE_PROMPT = r"""
你是一名专业的 OCR/VLM 信息抽取助手. 你需要从文档中提取 schema 中给定的字段, 并严格
输出 JSON.

输入说明:
- 你收到的是一组按顺序排列的图片, 每张图对应原文档的一页;
- 请按页码顺序阅读, 注意跨页的内容衔接;
- 图片经过压缩处理, 请仔细辨认文字内容.

输出要求:
- 只能输出 JSON, 不要输出任何解释 / 说明 / markdown 代码块;
- JSON 中必须包含 schema 里所有字段, 即便对应内容在文档里找不到也要给出明确的空值
  (而不是缺字段);
- 单字段内容跨多行时用 '\n' 连接, 保留原文换行结构.
"""

PDF_MARKDOWN_PROMPT = r"""
你是一名专业的 OCR/VLM 信息抽取助手. 你需要从文档中提取 schema 中给定的字段, 并严格
输出 JSON.

输入说明:
- 你收到的是文档的结构化 markdown 内容，包含标题、段落、表格等结构信息；
- 同时附有文档页面的图片，用于辅助理解复杂排版和图表；
- 请以 markdown 为主要参考，图片作为补充验证。

输出要求:
- 只能输出 JSON, 不要输出任何解释 / 说明 / markdown 代码块;
- JSON 中必须包含 schema 里所有字段, 即便对应内容在文档里找不到也要给出明确的空值
  (而不是缺字段);
- 单字段内容跨多行时用 '\n' 连接, 保留原文换行结构.
"""

_PROMPTS_BY_MODE = {
    PdfMode.NATIVE: PDF_PROMPT,
    PdfMode.IMAGE: PDF_IMAGE_PROMPT,
    PdfMode.MARKDOWN: PDF_MARKDOWN_PROMPT,
}


def _ensure_pymupdf_layout_int64_patch() -> None:
    """修补部分 PyMuPDF / pymupdf4llm layout 版本的 ONNX int32/int64 入参不匹配问题。"""
    global _PYMUPDF_LAYOUT_PATCHED
    if _PYMUPDF_LAYOUT_PATCHED:
        return

    try:
        import numpy as np
        from pymupdf.layout.onnx import BoxRFDGNN as box_rfdgnn
        original = box_rfdgnn.get_nn_input_from_datadict
    except (AttributeError, ImportError):
        return

    if getattr(original, "_apdfi_int64_patched", False):
        _PYMUPDF_LAYOUT_PATCHED = True
        return

    def patched_get_nn_input(*args: Any, **kwargs: Any):
        x, edge_index, edge_attr, nn_index, nn_attr, rf, tf, imf, crop = original(*args, **kwargs)
        edge_index = np.asarray(edge_index, dtype=np.int64)
        if nn_index is not None:
            nn_index = np.asarray(nn_index, dtype=np.int64)
        return x, edge_index, edge_attr, nn_index, nn_attr, rf, tf, imf, crop

    patched_get_nn_input._apdfi_int64_patched = True
    box_rfdgnn.get_nn_input_from_datadict = patched_get_nn_input
    _PYMUPDF_LAYOUT_PATCHED = True


def _supports_native_pdf(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in _NATIVE_PDF_MODEL_PREFIXES)


def _coerce_mode(mode: PdfMode | str) -> PdfMode:
    try:
        return mode if isinstance(mode, PdfMode) else PdfMode(mode)
    except ValueError as e:
        allowed = ", ".join(item.value for item in PdfMode)
        raise ValueError(f"Unsupported PDF mode `{mode}`. Allowed values: {allowed}.") from e


def _resolve_mode(mode: PdfMode | str, model: str) -> PdfMode:
    requested = _coerce_mode(mode)
    if requested != PdfMode.AUTO:
        return requested
    return PdfMode.NATIVE if _supports_native_pdf(model) else PdfMode.IMAGE


def _data_uri(mime_type: str, payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode()
    return f"data:{mime_type};base64,{encoded}"


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _image_block(jpeg_bytes: bytes) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": _data_uri("image/jpeg", jpeg_bytes)},
    }


def _pdf_file_block(pdf_bytes: bytes) -> dict:
    return {
        "type": "file",
        "file": _data_uri("application/pdf", pdf_bytes),
    }


def _render_one_page(
    pdf_bytes: bytes,
    page_no: int,
    mat: fitz.Matrix,
    grayscale: bool,
    jpeg_quality: int,
    max_dim: int | None,
) -> bytes:
    """在独立线程中将单页渲染为 JPEG bytes。每次独立打开文档，线程安全。"""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        cs = fitz.csGRAY if grayscale else fitz.csRGB
        pix = doc[page_no].get_pixmap(matrix=mat, alpha=False, colorspace=cs)

    # 只在需要 resize 时走 PIL，否则直接用 PyMuPDF 编码节省 CPU
    if max_dim and (pix.width > max_dim or pix.height > max_dim):
        mode = "L" if grayscale else "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        return buf.getvalue()

    return pix.tobytes(output="jpg", jpg_quality=jpeg_quality)


def _pdf_to_image_blocks(
    pdf_bytes: bytes,
    dpi: int = 150,
    jpeg_quality: int = 85,
    max_dim: int = 2048,
    grayscale: bool = False,
    page_start: int = 0,
    page_end: int | None = None,
) -> list[dict]:
    """将 PDF 页面并发渲染为 JPEG 图片，编码为 OpenAI-compatible image content blocks。

    Args:
        dpi: 渲染分辨率，默认 150 DPI（对文字识别已足够，比 200 DPI 节省约 44% 体积）。
        jpeg_quality: JPEG 压缩质量 1-95。
        max_dim: 单边最大像素数，超出时等比缩放（减少 VLM token 消耗）。
        grayscale: 转灰度模式，文字型 PDF 可减少约 60% 体积。
        page_start: 起始页（0-indexed，含）。
        page_end: 结束页（0-indexed，不含），None 表示到最后一页。
    """
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        total = len(doc)

    end = min(page_end, total) if page_end is not None else total
    page_indices = list(range(max(0, page_start), end))
    mat = fitz.Matrix(dpi / 72, dpi / 72)

    with ThreadPoolExecutor(max_workers=min(len(page_indices), 8)) as pool:
        jpeg_list = list(pool.map(
            lambda i: _render_one_page(pdf_bytes, i, mat, grayscale, jpeg_quality, max_dim),
            page_indices,
        ))

    return [_image_block(jpeg) for jpeg in jpeg_list]


def _is_onnx_int_dtype_error(exc: Exception) -> bool:
    message = str(exc)
    return "Unexpected input data type" in message and "tensor(int32)" in message and "tensor(int64)" in message


def _pymupdf4llm_to_markdown(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return pymupdf4llm.to_markdown(doc)
    finally:
        doc.close()


def _pdf_to_markdown(pdf_bytes: bytes) -> str:
    """使用 pymupdf4llm 将 PDF 转换为结构化 markdown。"""
    _ensure_pymupdf_layout_int64_patch()
    try:
        return _pymupdf4llm_to_markdown(pdf_bytes)
    except Exception as e:
        if not _is_onnx_int_dtype_error(e) or not hasattr(pymupdf4llm, "use_layout"):
            raise
        pymupdf4llm.use_layout(False)
        return _pymupdf4llm_to_markdown(pdf_bytes)


def _build_content(
    pdf_bytes: bytes,
    prompt: str,
    mode: PdfMode,
    *,
    dpi: int = 150,
    jpeg_quality: int = 85,
    max_dim: int = 2048,
    grayscale: bool = False,
    page_start: int = 0,
    page_end: int | None = None,
) -> list[dict]:
    content = [_text_block(prompt)]

    _img_kwargs = dict(
        dpi=dpi,
        jpeg_quality=jpeg_quality,
        max_dim=max_dim,
        grayscale=grayscale,
        page_start=page_start,
        page_end=page_end,
    )

    if mode == PdfMode.NATIVE:
        return [*content, _pdf_file_block(pdf_bytes)]

    if mode == PdfMode.MARKDOWN:
        md_text = _pdf_to_markdown(pdf_bytes)
        return [
            *content,
            _text_block(f"以下为文档的结构化内容：\n\n{md_text}"),
            *_pdf_to_image_blocks(pdf_bytes, **_img_kwargs),
        ]

    return [*content, *_pdf_to_image_blocks(pdf_bytes, **_img_kwargs)]


async def ask_vlm(
    pdf_bytes: bytes,
    schema: type[T],
    *,
    model: str = DEFAULT_MODEL,
    prompt: str | None = None,
    retry: int = 2,
    mode: PdfMode | str = PdfMode.AUTO,
    dpi: int = 150,
    jpeg_quality: int = 85,
    max_dim: int = 2048,
    grayscale: bool = False,
    page_start: int = 0,
    page_end: int | None = None,
) -> dict:
    """
    用 VLM 从 PDF 中抽取结构化数据。

    mode:
        - auto: native-PDF 模型用 native, 其他模型降级 image
        - native: 直接发送 PDF 文件
        - image: 将 PDF 渲染为图片发送
        - markdown: 用 pymupdf4llm 提取 markdown, 并附页面图片辅助理解

    image mode 参数（mode=image/markdown/auto 降级时生效）:
        dpi: 渲染分辨率，默认 150 DPI
        jpeg_quality: JPEG 质量 1-95，默认 85
        max_dim: 单边最大像素，默认 2048，超出等比缩放
        grayscale: 是否转灰度（文字型 PDF 可节省约 60% 体积）
        page_start: 起始页（0-indexed，含）
        page_end: 结束页（0-indexed，不含），None 表示到最后一页
    """
    resolved_mode = _resolve_mode(mode, model)
    if prompt is None:
        prompt = _PROMPTS_BY_MODE[resolved_mode]

    content = await run_in_threadpool(
        _build_content,
        pdf_bytes,
        prompt,
        resolved_mode,
        dpi=dpi,
        jpeg_quality=jpeg_quality,
        max_dim=max_dim,
        grayscale=grayscale,
        page_start=page_start,
        page_end=page_end,
    )

    messages = [{
        "role": "user",
        "content": content,
    }]

    obj = await instructor_call(schema, messages, model=model, retry=retry)
    return obj.model_dump(mode="json")
