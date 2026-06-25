"""通用异步 HTTP 客户端 + 注册表."""

import logging

import httpx

log = logging.getLogger(__name__)


class HttpClient:
    """通用 HTTP 客户端，包装 ``httpx.AsyncClient``."""

    def __init__(self, base_url: str, auth_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def get(self, path: str, params: dict | None = None) -> dict:
        """GET 请求."""
        client = await self._get_client()
        url = path if path.startswith("/") else f"/{path}"
        log.info(f"HTTP GET {self.base_url}{url} params={params}")
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def post(self, path: str, json_data: dict | None = None) -> dict:
        """POST 请求."""
        client = await self._get_client()
        url = path if path.startswith("/") else f"/{path}"
        log.info(f"HTTP POST {self.base_url}{url} data={json_data}")
        response = await client.post(url, json=json_data)
        response.raise_for_status()
        return response.json()

    async def close(self):
        """关闭客户端."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# 客户端注册表
_clients: dict[str, HttpClient] = {}


def register_client(name: str, base_url: str, auth_token: str | None = None):
    """注册一个 HTTP 客户端."""
    _clients[name] = HttpClient(base_url, auth_token)
    log.info(f"Registered HTTP client: {name} -> {base_url}")


def get_client(name: str) -> HttpClient:
    """获取已注册的 HTTP 客户端."""
    if name not in _clients:
        raise ValueError(f"HTTP client not found: {name}")
    return _clients[name]
