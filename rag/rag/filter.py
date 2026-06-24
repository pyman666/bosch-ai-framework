"""(过滤 / 检索前输入处理) 把外部请求 (URL + payload + 用户问题) 加工成检索两层各自需要的形式.

跟"检索"分开放是因为这些都是**纯文本/字符串变换**, 不碰索引, 不依赖任何模型. 把它们集中起来有两个好处:
  - 后续要给 retrieval 加新的 hint (e.g. 客户 ID → tenant 软加权), 改这一个文件就够;
  - Layer 1 / Layer 2 都能直接用同一份归一化的 URL, 不用各自做一遍.

提供三件事:

1. :func:`normalize_url`        — 砍掉 host / query / 末尾斜杠, 给两层 retrieve 用. 业务无关.
2. :func:`infer_module_from_url`— URL 第一段 → 已知模块名, 作为 hybrid 检索的软加权 hint.
                                  ``url_head_to_module`` 映射表由业务通过参数注入
                                  (空 dict → 不做模块推断, retriever 全模块兜底).
3. :func:`build_search_query`   — 把结构化 (URL + payload + 用户问题) 压成一条 token-friendly query 字符串.
                                  ``priority_keys`` (payload 里"诊断价值最高"的字段顺序)
                                  由业务通过参数注入.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# URL 归一 + 模块推断
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """对外部传来的 URL 做最低限度的归一: 去掉前缀域名 / query / 末尾斜杠.

    给 Layer 1 (路径模板匹配) 和模块推断共用. 不要在这里做更激进的归一 (e.g. 大小写折叠), 路径段大小写在 KB 里是有语义的.
    """
    if not url:
        return ""
    url = url.strip()
    # 砍掉 host (http(s)://host/...) 只保留 path
    url = re.sub(r"^https?://[^/]+", "", url)
    # 砍掉 query / fragment
    url = url.split("?", 1)[0].split("#", 1)[0]
    if not url.startswith("/"):
        url = "/" + url
    if len(url) > 1 and url.endswith("/"):
        url = url[:-1]
    return url


def infer_module_from_url(
    route_url: str,
    url_head_to_module: Mapping[str, str],
) -> str | None:
    """从 URL 第一段粗推 module — 用于后续 retrieval 做软加权, 缩候选集.

    URL 一般形如 ``/<head>/...``; ``url_head_to_module`` 是业务侧给的"URL
    第一段 (lower-case) → 模块名"字典. 不在表里的前缀回 None,
    让检索器全模块兜底.

    传入空字典 = 不启用模块推断, 等价于始终回 None.
    """
    url = normalize_url(route_url)
    if not url or url == "/":
        return None
    head = url.strip("/").split("/", 1)[0].lower()
    return url_head_to_module.get(head)


# ---------------------------------------------------------------------------
# 检索 query 拼装
# ---------------------------------------------------------------------------

def build_search_query(
    route_url: str,
    payload: dict[str, Any],
    user_question: str | None,
    *,
    priority_keys: tuple[str, ...] = (),
) -> str:
    """把结构化输入压成给 hybrid retriever 的 query 字符串.

    BM25 / dense embedding 都是 token 级的, 直接灌整段 payload json 一会儿
    被截断一会儿被噪声稀释. 这里:
      - ``route_url`` 在最前 (它带强信号: 路径段往往含 customer / endpoint);
      - 对 payload 按 ``priority_keys`` 顺序优先取"诊断价值最高"的字段
        (业务定义, e.g. processRemark / processStatus / errorCode);
      - 其余标量字段以 ``key=value`` 形式追加 (跳过已经放过的 priority_keys);
      - ``user_question`` 放最后.

    ``priority_keys`` 为空 = 不做优先级, payload 字段按 dict 自然顺序追加.
    """
    parts: list[str] = [route_url]
    if not payload:
        payload = {}

    for key in priority_keys:
        v = payload.get(key)
        if v not in (None, ""):
            parts.append(f"{key}={v}")

    seen = set(priority_keys)
    for k, v in payload.items():
        if k in seen:
            continue
        if isinstance(v, (str, int, float, bool)) and v != "":
            parts.append(f"{k}={v}")

    if user_question:
        parts.append(user_question)

    return " | ".join(str(p) for p in parts)
