"""PDF OCR 可视化 pipeline (PaddleOCR 实现, 后续可能换).

**懒加载**: 这里**故意不在模块顶层 import paddleocr / paddlex**, 因为:
- ``PaddleOCR()`` 实例化 + 模型权重首次下载/加载是 30+ 秒重操作.
- 仅 ``ocr_image=True`` 这条可选路径才用得到, 大部分 PDF 请求根本不走 OCR.
- 后续大概率换掉 paddle 方案, 留个干净的 lazy 边界方便替换.

首次调用 ``map_ocr`` 时再走 ``_ensure_paddle_loaded`` 一次性初始化, 后续命中缓存.
想在 server 起来前预热, 调用方自己显式调一次即可.
"""
import re
import sys
import asyncio
import types
import threading
import numpy as np
import fitz
from copy import deepcopy
from PIL import Image
from fastapi.concurrency import run_in_threadpool
from .text import fuzz_extract


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


_paddle_ready = False
_paddle_init_lock = threading.Lock()
_ocr = None  # PaddleOCR 实例, 首次调用时由 _ensure_paddle_loaded 填好.
_ocr_lock = asyncio.Lock()  # 串行 predict, 避免一个 PaddleOCR 跨线程并发.


def _ensure_paddle_loaded():
    """线程安全的 paddleocr 懒加载. 返回单例 ``PaddleOCR``.

    第一次调用会:
        1. 给 paddleocr 装好 langchain >=1.0 的兼容垫片
           (https://github.com/PaddlePaddle/PaddleOCR/issues/16711);
        2. ``import paddleocr``;
        3. ``PaddleOCR()`` 实例化 + 模型权重加载.

    这一坨整体在 30s+, 因此调用方一般通过 ``run_in_threadpool(_ensure_paddle_loaded)``
    在异步上下文里跑, 不阻塞 event loop.
    """
    global _ocr, _paddle_ready
    if _paddle_ready:
        return _ocr
    with _paddle_init_lock:
        if _paddle_ready:
            return _ocr

        import langchain_core
        if _version_tuple(langchain_core.__version__) > (1, 0, 0):
            from langchain_core.documents import Document
            from langchain_text_splitters import RecursiveCharacterTextSplitter

            m1 = types.ModuleType("langchain.docstore.document")
            m1.Document = Document
            sys.modules["langchain.docstore.document"] = m1

            m2 = types.ModuleType("langchain.text_splitter")
            m2.RecursiveCharacterTextSplitter = RecursiveCharacterTextSplitter
            sys.modules["langchain.text_splitter"] = m2

        from paddleocr import PaddleOCR  # noqa: E402  (must follow langchain shim)

        _ocr = PaddleOCR()
        _paddle_ready = True
    return _ocr


async def async_ocr(
    pdf_bytes: bytes,
    ocr=None,
    dpi: int = 300,
) -> list:
    """对 PDF 每页跑 OCR. ``ocr`` 不传则懒加载单例.

    返回 ``list[paddlex.inference.pipelines.ocr.result.OCRResult]``, 因为类型 import
    本身也是重操作, 这里只标 ``list``.
    """
    if ocr is None:
        ocr = await run_in_threadpool(_ensure_paddle_loaded)

    async with _ocr_lock:
        def sync_task():
            results = []
            with fitz.open(stream=pdf_bytes, filetype="pdf") as pages:
                for page in pages:
                    pix = page.get_pixmap(dpi=dpi)

                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    img_np = np.array(img)
                    result = ocr.predict(img_np)[0]

                    results.append(result)
            return results

        return await run_in_threadpool(sync_task)


async def map_ocr(
    vlm_result,
    pdf_bytes: bytes,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    **kwargs,
) -> list[Image.Image]:
    imgs = []
    ocr_results = await async_ocr(pdf_bytes)

    for ocr_result in ocr_results:
        result = deepcopy(ocr_result)

        rec_texts = ocr_result["rec_texts"]
        rec_polys = ocr_result["rec_polys"]

        result["doc_preprocessor_res"] = ocr_result["doc_preprocessor_res"]
        result["rec_texts"] = []
        result["rec_polys"] = []

        def _walk(obj, key=""):
            if (
                not obj
                or (exclude and any(i.startswith(key) for i in exclude))
                or (include and not any(i.startswith(key) for i in include))
            ):
                return None
            elif isinstance(obj, dict):
                return {k: _walk(v, f"{key}.{k}" if key else k) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple, set)):
                return [_walk(v, f"{key}[{i}]") for i, v in enumerate(obj)]
            else:
                value = str(obj).strip()
                best = fuzz_extract(value, rec_texts, **kwargs)
                if best:
                    text, score, idx = best
                    result["rec_texts"].append(f"{value} (ocr: {text})")
                    result["rec_polys"].append(rec_polys[idx])
            return None

        _walk(vlm_result)
        imgs.append(result.img.get("ocr_res_img"))

    return imgs
