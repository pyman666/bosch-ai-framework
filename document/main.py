from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import ResponseValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from document.auth import require_auth
from document.utils import exception_detail
from document.pdf import router as pdf_router
from document.excel import router as excel_router


_app_description = """
## 📄 智能 PDF / Excel 解析服务

基于 LLM 的 PDF 字段抽取 + 复杂业务 Excel 解析。

### PDF
LLM 字段抽取, 可选文本校验与 OCR 区域可视化。

### Excel
LLM excel 解析, 可在 chat 中修正解析结果。

### 鉴权
所有业务端点走 HTTP Basic Auth。
"""


app = FastAPI(
    title="智能文档解析 API",
    description=_app_description,
    version="0.1.0",
    license_info={"name": "Learning Purposes Only"},
    contact={
        "name": "HN",
        "email": "hn_1992@163.com",
    },
    openapi_tags=[
        {"name": "PDF", "description": "PDF 字段抽取"},
        {"name": "EXCEL", "description": "复杂业务 Excel 解析"},
    ],
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
)

# 鉴权统一在 mount 这一行注入, 而不是每个域 router 自己声明 ``dependencies=...``.
# 一处即可俯瞰 "所有业务端点都受 require_auth 保护"; 静态 /mock 目录在下方 mount,
# 不带这个 dep, 浏览器打开 mock 不弹 basic auth 框 (mock 内部 fetch 业务端点时
# 自己带 Authorization header).
_business_auth = [Depends(require_auth)]
app.include_router(pdf_router, prefix="/pdf", tags=["PDF"], dependencies=_business_auth)
app.include_router(excel_router, prefix="/excel", tags=["EXCEL"], dependencies=_business_auth)


# ---------------------------------------------------------------------------
# /mock 静态目录: 项目根 docs/ 下放 demo HTML (e.g. aidoc-mock.html), server
# 启动后浏览器直接 http://<host>:<port>/mock/aidoc-mock.html 即可访问. 同源 +
# 不挂 ``Depends(require_auth)``, 避免 mock 打开时浏览器弹 basic auth 框. mock
# 内部 fetch 业务端点时自己带 Authorization header (mock 顶部有 secret 输入框).
# 生产部署时如果不想暴露 mock, 把这一段 mount 注释掉即可.
_DOCS_DIR = Path(__file__).resolve().parent / "docs"
if _DOCS_DIR.is_dir():
    app.mount("/mock", StaticFiles(directory=str(_DOCS_DIR), html=True), name="mock")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=getattr(exc, "status_code", 500),
        content={"detail": jsonable_encoder(exception_detail(exc))},
    )

app.add_exception_handler(ResponseValidationError, unhandled_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
