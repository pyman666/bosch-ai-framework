"""轻量级 POC 限流：IP 滑动窗口 + 重计算 Skill 并发控制。

生产环境建议替换为 Redis-based 实现。
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 重计算 Skill 识别
# ---------------------------------------------------------------------------

# 已知吃资源的预设名称
_HEAVY_PRESETS: set[str] = {
    "arima", "holt_winters", "chronos", "zero_shot",
}

# Python skill 中包含这些 import 则视为重计算
_HEAVY_IMPORTS: tuple[str, ...] = ("statsmodels",)


def is_heavy_skill(
    skill_type: str,
    preset_name: str | None = None,
    python_code: str | None = None,
) -> bool:
    """判断一个 skill 是否为重计算类型（ARIMA、Holt-Winters 等）。"""
    if skill_type == "preset" and preset_name and preset_name in _HEAVY_PRESETS:
        return True
    if skill_type == "python" and python_code:
        return any(imp in python_code for imp in _HEAVY_IMPORTS)
    return False


# ---------------------------------------------------------------------------
# IP 滑动窗口限流
# ---------------------------------------------------------------------------

# 默认配置：每 IP 每分钟最多 60 请求（可通过环境变量覆盖）
_RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
_RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "60"))

# IP -> deque of timestamps
_buckets: dict[str, list[float]] = defaultdict(list)


def _cleanup_bucket(ip: str, now: float) -> int:
    """清理过期请求，返回当前窗口内剩余请求数。"""
    if ip not in _buckets:
        return _RATE_LIMIT_MAX
    cutoff = now - _RATE_LIMIT_WINDOW
    _buckets[ip] = [t for t in _buckets[ip] if t > cutoff]
    remaining = _RATE_LIMIT_MAX - len(_buckets[ip])
    return max(0, remaining)


def _record_request(ip: str, now: float) -> None:
    _buckets[ip].append(now)


def _get_rate_limit_headers(ip: str, now: float) -> dict[str, str]:
    remaining = _cleanup_bucket(ip, now)
    reset_time = int(now + _RATE_LIMIT_WINDOW)
    return {
        "X-RateLimit-Limit": str(_RATE_LIMIT_MAX),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_time),
    }


class RateLimitMiddleware(BaseHTTPMiddleware):
    """IP 滑动窗口限流中间件。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        # 只限制 forecast 端点
        if not request.url.path.startswith("/api/v1/forecast"):
            return await call_next(request)

        now = time.monotonic()
        ip = request.client.host if request.client else "unknown"
        headers = _get_rate_limit_headers(ip, now)

        remaining = int(headers["X-RateLimit-Remaining"])
        if remaining <= 0:
            retry_after = _RATE_LIMIT_WINDOW
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后重试", "retry_after": retry_after},
                headers=headers,
            )

        _record_request(ip, now)
        response = await call_next(request)
        for k, v in headers.items():
            response.headers[k] = v
        return response
