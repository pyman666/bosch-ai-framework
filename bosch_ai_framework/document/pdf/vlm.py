"""PDF VLM extraction — 4-mode PDF parsing via vision language models.

Modes:
    - ``auto``: native-PDF models (Claude/Gemini) → native; others → image
    - ``native``: send PDF file bytes directly
    - ``image``: render PDF pages to JPEG, send as images
    - ``markdown``: extract markdown via pymupdf4llm + page images as supplement
"""

from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from io import BytesIO
from typing import Any, TypeVar

import fitz  # pymupdf
import pymupdf4llm
from PIL import Image
from pydantic import BaseModel

from bosch_ai_framework.llm.instructor import instructor_call

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_NATIVE_PDF_MODEL_PREFIXES = ("claude-", "gemini-")
_PYMUPDF_LAYOUT_PATCHED = False

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PDF_PROMPT = """You are a professional OCR/VLM information extraction assistant. \
Extract the fields specified in the schema from the PDF and output strictly as JSON.

Output requirements:
- Output ONLY JSON, no explanations, notes, or markdown code blocks
- Include ALL fields from the schema — return explicit nulls for missing fields, don't omit them
- For multi-line field content, join with '\\n', preserving original line breaks"""

PDF_IMAGE_PROMPT = """You are a professional OCR/VLM information extraction assistant. \
Extract the fields specified in the schema from the document and output strictly as JSON.

Input notes:
- You receive images in page order; read sequentially, noting cross-page content
- Images are compressed; examine text carefully

Output requirements:
- Output ONLY JSON, no explanations or markdown code blocks
- Include ALL schema fields — return explicit nulls for missing fields
- Join multi-line content with '\\n'"""

PDF_MARKDOWN_PROMPT = """You are a professional OCR/VLM information extraction assistant. \
Extract the fields specified in the schema from the document and output strictly as JSON.

Input notes:
- You receive structured markdown (headings, paragraphs, tables) as primary reference
- Page images are attached as supplementary visual aid for complex layouts

Output requirements:
- Output ONLY JSON, no explanations or markdown code blocks
- Include ALL schema fields — return explicit nulls for missing fields
- Join multi-line content with '\\n'"""

_PROMPTS_BY_MODE: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


class PdfMode(str, Enum):
    AUTO = "auto"
    NATIVE = "native"
    IMAGE = "image"
    MARKDOWN = "markdown"


def _supports_native_pdf(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in _NATIVE_PDF_MODEL_PREFIXES)


def _coerce_mode(mode: PdfMode | str) -> PdfMode:
    try:
        return mode if isinstance(mode, PdfMode) else PdfMode(mode)
    except ValueError as e:
        allowed = ", ".join(item.value for item in PdfMode)
        raise ValueError(f"Unsupported PDF mode `{mode}`. Allowed: {allowed}.") from e


def _resolve_mode(mode: PdfMode | str, model: str) -> PdfMode:
    requested = _coerce_mode(mode)
    if requested != PdfMode.AUTO:
        return requested
    return PdfMode.NATIVE if _supports_native_pdf(model) else PdfMode.IMAGE


# ---------------------------------------------------------------------------
# Content builders
# ---------------------------------------------------------------------------


def _data_uri(mime_type: str, payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode()
    return f"data:{mime_type};base64,{encoded}"


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _image_block(jpeg_bytes: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": _data_uri("image/jpeg", jpeg_bytes)}}


def _pdf_file_block(pdf_bytes: bytes) -> dict:
    return {"type": "file", "file": _data_uri("application/pdf", pdf_bytes)}


def _render_one_page(
    pdf_bytes: bytes,
    page_no: int,
    mat: fitz.Matrix,
    grayscale: bool,
    jpeg_quality: int,
    max_dim: int | None,
) -> bytes:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        cs = fitz.csGRAY if grayscale else fitz.csRGB
        pix = doc[page_no].get_pixmap(matrix=mat, alpha=False, colorspace=cs)

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


def _ensure_pymupdf_layout_int64_patch() -> None:
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


def _pymupdf4llm_to_markdown(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return pymupdf4llm.to_markdown(doc)
    finally:
        doc.close()


def _pdf_to_markdown(pdf_bytes: bytes) -> str:
    _ensure_pymupdf_layout_int64_patch()
    try:
        return _pymupdf4llm_to_markdown(pdf_bytes)
    except Exception as e:
        if not _is_onnx_int_dtype_error(e) or not hasattr(pymupdf4llm, "use_layout"):
            raise
        pymupdf4llm.use_layout(False)
        return _pymupdf4llm_to_markdown(pdf_bytes)


def _is_onnx_int_dtype_error(exc: Exception) -> bool:
    message = str(exc)
    return "Unexpected input data type" in message and "tensor(int32)" in message and "tensor(int64)" in message


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

    img_kwargs = dict(dpi=dpi, jpeg_quality=jpeg_quality, max_dim=max_dim,
                      grayscale=grayscale, page_start=page_start, page_end=page_end)

    if mode == PdfMode.NATIVE:
        return [*content, _pdf_file_block(pdf_bytes)]

    if mode == PdfMode.MARKDOWN:
        md_text = _pdf_to_markdown(pdf_bytes)
        return [*content, _text_block(f"Document structured content:\n\n{md_text}"),
                *_pdf_to_image_blocks(pdf_bytes, **img_kwargs)]

    return [*content, *_pdf_to_image_blocks(pdf_bytes, **img_kwargs)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ask_vlm(
    pdf_bytes: bytes,
    schema: type[T],
    *,
    model: str,
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
    """Extract structured data from a PDF using a VLM.

    Args:
        pdf_bytes: Raw PDF file bytes.
        schema: Pydantic model class defining the extraction schema.
        model: LLM model name (e.g. ``"gpt-4o"``, ``"claude-sonnet-4-6"``).
        prompt: Custom extraction prompt. Auto-generated per mode if not provided.
        retry: Max retries on API error.
        mode: ``auto`` / ``native`` / ``image`` / ``markdown``.
        dpi: Render DPI (image/markdown modes), default 150.
        jpeg_quality: JPEG quality 1-95, default 85.
        max_dim: Max pixel dimension per page, default 2048.
        grayscale: Convert to grayscale (saves ~60% size for text PDFs).
        page_start: Start page (0-indexed, inclusive).
        page_end: End page (0-indexed, exclusive), None = last page.

    Returns:
        Dict from ``schema.model_dump(mode="json")``.
    """
    from fastapi.concurrency import run_in_threadpool

    resolved_mode = _resolve_mode(mode, model)
    if prompt is None:
        prompt = _PROMPTS_BY_MODE.get(resolved_mode, PDF_PROMPT)

    content = await run_in_threadpool(
        _build_content, pdf_bytes, prompt, resolved_mode,
        dpi=dpi, jpeg_quality=jpeg_quality, max_dim=max_dim,
        grayscale=grayscale, page_start=page_start, page_end=page_end,
    )

    messages = [{"role": "user", "content": content}]
    obj = await instructor_call(schema, messages, model=model, retry=retry)
    return obj.model_dump(mode="json")
