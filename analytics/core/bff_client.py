"""BFF HTTP 客户端."""

import logging

import httpx

log = logging.getLogger(__name__)


class BFFClient:
    """通用 BFF HTTP 客户端."""

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
        log.info(f"BFF GET {self.base_url}{url} params={params}")
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def post(self, path: str, json_data: dict | None = None) -> dict:
        """POST 请求."""
        client = await self._get_client()
        url = path if path.startswith("/") else f"/{path}"
        log.info(f"BFF POST {self.base_url}{url} data={json_data}")
        response = await client.post(url, json=json_data)
        response.raise_for_status()
        return response.json()

    async def close(self):
        """关闭客户端."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# BFF 注册表（后续从配置加载）
_bff_registry: dict[str, BFFClient] = {}


def register_bff(name: str, base_url: str, auth_token: str | None = None):
    """注册 BFF."""
    _bff_registry[name] = BFFClient(base_url, auth_token)
    log.info(f"Registered BFF: {name} -> {base_url}")


def get_bff(name: str) -> BFFClient:
    """获取 BFF 客户端."""
    if name not in _bff_registry:
        raise ValueError(f"BFF not found: {name}")
    return _bff_registry[name]
