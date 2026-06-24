"""健康检查接口."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查."""
    return {"status": "ok", "service": "abi-gateway"}
