"""(检索 Layer 2) BM25 (sparse) + FAISS (dense) + RRF 融合 + 可选
cross-encoder rerank.

Layer 1 (deterministic lookup) 在 :mod:`.retrieve_lookup`.
Layer 2 在这里.
由 :mod:`.pipeline` 把两层串起来 (Layer 1 出 ``excluded_ids``, Layer 2 把这些 chunk 排除掉避免 prompt 里重复).

为什么 BM25 + Dense 一起用:

  - BM25: **专有术语 / 错误字符串 / 路径片段** (e.g. ``BYD_BN`` / ``ReleaseToFIN`` / ``Mapping not found``) 在通用语料里几乎没出现过,
    通用 dense embedding 模型 (MiniLM) 的向量分不开它们; BM25 直接靠词频精确命中, 在这类查询上召回率显著高于 dense.
  - Dense: 用"白话"问 (e.g. "为什么我提交完没动静") 时, 字面 token 跟 KB 几乎不重叠, 这时 dense 才能靠语义把"释放/匹配状态机"召回.

合并用 **Reciprocal Rank Fusion (RRF)**: ``score = Σ 1/(k + rank)``, k=60.
RRF 不需要校准两层的分数尺度, 对 outlier 不敏感, 比加权和稳定得多.

可选 rerank: 上面拿出来 top-N (e.g. 30) 后, 用一个小 cross-encoder (``BAAI/bge-reranker-base``) 重排, 取 top-K 进 prompt.
在 1k 量级 chunk 上通常能再加 5~15% NDCG. 因为模型要 ~280MB, 默认关, 由构造参数打开.

综上:
    | 组件           | 作用            |
    | ------------- | --------------- |
    | BM25          | 关键词召回       |
    | FAISS         | 语义召回         |
    | RRF           | 融合多个召回结果 |
    | Cross-encoder | 精细重排序       |

本模块**零业务**: 不读任何 settings, 所有可调旋钮通过构造参数 / 方法参数传入.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import faiss
import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from .corpus import Chunk


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tokenizer — BM25 用
# ---------------------------------------------------------------------------

# 英文 / 代码标识符 (alnum + 下划线) 整词; 中文按字切分. 对我们的 KB 够用 —
# 高信号 token 几乎都在英文那一侧 (路径片段 / 类名 / 状态值 / 错误文本),
# 中文字符级 BM25 不算理想但不需要 jieba 这种重依赖.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


# ---------------------------------------------------------------------------
# RRF 融合
# ---------------------------------------------------------------------------

_RRF_K = 60


def _rrf_merge(
    *ranked_lists: list[int],
    k: int = _RRF_K,
) -> list[tuple[int, float]]:
    """对若干排名好的 chunk 索引列表做 RRF 融合, 返回 ``[(idx, score), ...]``,
    按合并 score 降序. 没出现过的 chunk 不进结果."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, idx in enumerate(ranked):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


# ---------------------------------------------------------------------------
# Reranker (可选, lazy load)
# ---------------------------------------------------------------------------

_DEFAULT_RERANKER = "BAAI/bge-reranker-base"


@dataclass
class _LazyReranker:
    """延迟加载的 cross-encoder. 加载失败时把 ``model`` 标 None 永久关掉,
    不影响主流程."""

    model_name: str
    _model: object | None = None
    _tried: bool = False

    def score(self, pairs: list[tuple[str, str]]) -> list[float] | None:
        if self._tried and self._model is None:
            return None
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder

                logger.info("loading reranker: %s", self.model_name)
                self._model = CrossEncoder(self.model_name)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "reranker '%s' load failed (%s); 关闭 rerank, hybrid 退化为 BM25+Dense+RRF",
                    self.model_name,
                    e,
                )
                self._model = None
                self._tried = True
                return None
            self._tried = True
        scores = self._model.predict(pairs)  # type: ignore[union-attr]
        return [float(s) for s in scores]


# ---------------------------------------------------------------------------
# HybridRetriever
# ---------------------------------------------------------------------------

_DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"


class HybridRetriever:
    """启动期一次性建好 dense + sparse 两个索引, 运行期做 hybrid 检索.

    设计点:
      - chunks 不变, 索引位置就是 chunks 列表的下标, 跨索引天然对齐.
      - dense 编码 ``title`` (breadcrumb 高密度摘要), BM25 索引 ``title + body`` (拿全文里的 token 全做命中). 两者职责不同, 不互相替代.
      - module_filter 是 *boost* 而不是硬过滤 — 用 URL 推断的 module 错的时候 (e.g. 客户问的是另一模块) 仍能拿到结果, 只是优先级低.
    """

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        embed_model_name: str = _DEFAULT_EMBED_MODEL,
        enable_rerank: bool = False,
        reranker_name: str = _DEFAULT_RERANKER,
    ) -> None:
        if not chunks:
            raise ValueError("HybridRetriever 需要非空 chunks 列表")
        self.chunks: list[Chunk] = list(chunks)
        self._build_dense(embed_model_name)
        self._build_bm25()
        self._reranker = _LazyReranker(reranker_name) if enable_rerank else None

    # ---------- index build ----------

    def _build_dense(self, model_name: str) -> None:
        titles = [c.title or c.section or c.body[:120] for c in self.chunks]
        self._embed_model = SentenceTransformer(model_name)
        embeddings = np.asarray(self._embed_model.encode(titles, convert_to_numpy=True))
        embeddings = embeddings.astype("float32")
        self._dense = faiss.IndexFlatL2(embeddings.shape[1])
        self._dense.add(embeddings)

    def _build_bm25(self) -> None:
        corpus = [_tokenize(f"{c.title}\n{c.body}") for c in self.chunks]
        # rank-bm25 不接受空文档, 给一个 sentinel token 占位 (空 chunk 应该
        # 早就被 corpus.py 过滤掉, 这里只是 belt-and-suspenders).
        corpus = [doc or ["__empty__"] for doc in corpus]
        self._bm25 = BM25Okapi(corpus)

    # ---------- query ----------

    def _dense_top(self, query: str, n: int) -> list[int]:
        qv = np.asarray(self._embed_model.encode([query], convert_to_numpy=True)).astype("float32")
        n = min(n, len(self.chunks))
        _, idx = self._dense.search(qv, n)
        return [int(i) for i in idx[0] if i >= 0]

    def _bm25_top(self, query: str, n: int) -> list[int]:
        toks = _tokenize(query)
        if not toks:
            return []
        scores = self._bm25.get_scores(toks)
        n = min(n, len(self.chunks))
        # argpartition 取 top-n, 再按真实分数排序; 比 argsort 全排省一截时间.
        if n <= 0:
            return []
        if n >= len(scores):
            order = np.argsort(-scores)
        else:
            top_idx = np.argpartition(-scores, n - 1)[:n]
            order = top_idx[np.argsort(-scores[top_idx])]
        # 0 分的 chunk 是"完全没命中任一 token", 没必要进结果污染 RRF.
        return [int(i) for i in order if scores[i] > 0]

    def _maybe_rerank(
        self,
        query: str,
        candidates: list[int],
        top_k: int,
    ) -> list[int]:
        if self._reranker is None or len(candidates) <= top_k:
            return candidates[:top_k]
        pairs = [(query, self.chunks[i].body) for i in candidates]
        scores = self._reranker.score(pairs)
        if scores is None:
            return candidates[:top_k]
        order = np.argsort(-np.asarray(scores))
        return [candidates[int(i)] for i in order[:top_k]]

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 8,
        candidate_pool: int = 30,
        module_filter: str | None = None,
        exclude_chunk_ids: set[str] | None = None,
    ) -> list[Chunk]:
        """对 query 做 hybrid 检索, 返回 top-K 个 chunk.

        - ``candidate_pool``: 给 BM25 / Dense 各取多少候选进 RRF; 也是 rerank 的输入规模. 30 在 ~1k 语料上是 sweet spot.
        - ``module_filter``: 推断出的模块, 这里把同模块 chunk 的 RRF 分加权 1.5 (软加权, 不硬过滤, 让别的模块仍可作兜底).
        - ``exclude_chunk_ids``: 已经被 deterministic 命中过的 chunk, retrieval 这边跳过, 避免 prompt 里同一条 chunk 出现两次.
        """
        if not query:
            return []
        excluded = exclude_chunk_ids or set()

        dense_ranked = self._dense_top(query, candidate_pool)
        bm25_ranked = self._bm25_top(query, candidate_pool)
        merged = _rrf_merge(dense_ranked, bm25_ranked)

        if module_filter:
            merged = [
                (i, s * (1.5 if self.chunks[i].module == module_filter else 1.0))
                for i, s in merged
            ]
            merged.sort(key=lambda x: -x[1])

        merged_idxs = [i for i, _ in merged if self.chunks[i].chunk_id not in excluded]

        # rerank 输入用 candidate_pool 个, 输出 top_k
        topn = merged_idxs[:candidate_pool]
        chosen = self._maybe_rerank(query, topn, top_k)
        return [self.chunks[i] for i in chosen]
