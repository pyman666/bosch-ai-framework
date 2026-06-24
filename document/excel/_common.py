"""Excel 路由层共用的表单依赖 + 跨模块共享的 cell 工具函数."""
import pandas as pd
from fastapi import File, Form, UploadFile


# ---------------------------------------------------------------------------
# 共享 cell 工具 (simple / wide 两个引擎共用, 避免重复定义)


def _is_nullish(v: object) -> bool:
    """pandas 友好的"空"判断: None / NaN / NaT / 空白字符串都算空."""
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(v, str) and not v.strip()


def _clean_cell(v: object) -> object:
    """DataFrame 出来的 cell 清洗: NaN/NaT/空白 -> None, 其它原样 (JSON 序列化友好)."""
    return None if _is_nullish(v) else v


async def excel_upload(
    file: UploadFile = File(..., description="Excel 文件"),
    sheet_index: int = Form(1, description="sheet 序号 (1-based), 默认 `1`"),
    sheet_name: str = Form(None, description="sheet 名称, 设了则覆盖 sheet_index"),
) -> dict:
    """所有 Excel endpoint 都要的: 文件 + sheet 选择. 返回 raw bytes / sheet 标识 / 文件名.

    ``file_name`` 后续用来给前端拼"前瞻"文案 (``"我已经收到您上传的文件 xxx"``),
    业务路由层从这里拿就行, 不必再翻 ``UploadFile``.
    """
    raw = await file.read()
    return {
        "raw": raw,
        "sheet": sheet_name or sheet_index,
        "file_name": file.filename or "(未命名)",
    }
