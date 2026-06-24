import re
import asyncio
import string
import fitz
import unicodedata
from copy import deepcopy
from rapidfuzz import process
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool


INDEX = re.compile(r"\[\d+]")
DELETE_WHITESPACE = str.maketrans('', '', string.whitespace)

_pdf_lock = asyncio.Lock()


async def async_pdf(pdf_bytes: bytes) -> list[str]:
    async with _pdf_lock:
        def sync_task():
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                return [page.get_text("text") for page in doc]

        return await run_in_threadpool(sync_task)


def normalize(s: str) -> str:
    if not s:
        return ""

    s = unicodedata.normalize("NFKC", s)  # 全角转半角
    s = s.lower()
    s = s.translate(DELETE_WHITESPACE)
    s = "".join(c for c in s if c.isprintable())
    return s


def _extract_one(
    query,
    choices,
    *,
    processor=normalize,
    score_cutoff=75,
    **kwargs,
) -> tuple | None:
    results = process.extract(
        query,
        choices,
        limit=None,
        processor=processor,
        score_cutoff=score_cutoff,
        **kwargs,
    )
    if not results:
        return None

    best_score = results[0][1]
    top_candidates = [i for i in results if i[1] == best_score]

    if len(top_candidates) == 1:
        return top_candidates[0]

    len_query = len(query.translate(DELETE_WHITESPACE))

    def _length_diff(candidate):
        text, _, _ = candidate
        return abs(len(text.translate(DELETE_WHITESPACE)) - len_query)

    return min(top_candidates, key=_length_diff)


def fuzz_extract(
    query: str,
    choices: list,
    slide_result: bool = False,
    **kwargs,
) -> tuple | None:
    best = _extract_one(query, choices, **kwargs)
    if not best or not slide_result:
        return best

    match, _, _ = best
    len_match, window = len(match), len(query)
    choices_ = [match[i: i + window] for i in range(len_match - window + 1)]
    return _extract_one(query, choices_, **kwargs)


async def validate_text(
    vlm_result,
    pdf_bytes: bytes,
    *,
    exclude: list[str] | None = None,
    include: list[str] | None = None,
    **kwargs,
):
    errors = []
    pdf_texts = await async_pdf(pdf_bytes)
    pdf_text = "\n".join(pdf_texts)
    text_ = normalize(pdf_text)
    lines = [i for i in pdf_text.splitlines() if len(i) > 1]
    words = [i for i in pdf_text.split() if len(i) > 1]

    def _walk(obj, key=""):
        key_ = INDEX.sub("", key)
        if (
            not obj
            or (exclude and key_ in exclude)
            or (include and key_ not in include)
        ):
            return obj
        elif isinstance(obj, dict):
            return {k: _walk(v, f"{key}.{k}" if key else k) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_walk(v, f"{key}[{i}]") for i, v in enumerate(obj)]
        else:
            value = str(obj).strip()
            if not value or value in pdf_text or normalize(value) in text_:
                return value
            elif "\n" in value:
                if all(v in pdf_text or normalize(v) in text_ for v in value.split("\n")):
                    return value.replace("\n", " ")
            elif len(value) > 1:
                has_space = " " in value
                best = fuzz_extract(
                    value,
                    lines if has_space else words,
                    slide_result=has_space,
                    **kwargs,
                )
                if best:
                    return best[0]

            errors.append({
                "type": "ocr_check_failure",
                "loc": key,
                "input": value,
                "msg": f"字段 '{key}' 的值 '{value}' 在 PDF 中未找到",
            })
            return value

    data = _walk(deepcopy(vlm_result))
    if errors:
        raise HTTPException(status_code=500, detail=errors)
    return data
