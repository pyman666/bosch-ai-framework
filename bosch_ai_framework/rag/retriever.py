"""Hybrid retriever — BM25 (sparse) + FAISS (dense) + RRF fusion, optional rerank.

Why BM25 + Dense together:
    - BM25: domain-specific terms / error strings / path fragments (e.g. ``BYD_BN``,
      ``ReleaseToFIN``) that general dense models can't distinguish. BM25 hits them
      via exact token frequency.
    - Dense: natural-language questions (e.g. "why didn't my submission go through")
      where literal token overlap with the KB is low. Dense captures semantics.

Fusion via **Reciprocal Rank Fusion (RRF)**: ``score = Σ 1/(k + rank)``, k=60.
No scale calibration needed; robust to outliers.

Optional rerank: cross-encoder (``BAAI/bge-reranker-base``) re-ranks the RRF
candidate pool. Adds 5-15% NDCG on ~1k chunks. Disabled by default (~280MB model).

FAISS index persistence: startup checks cache dir for a matching content hash;
loads cached index if valid, otherwise rebuilds and saves.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from bosch_ai_framework.rag.corpus import Chunk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tokenizer — for BM25
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------

_RRF_K = 60


def _rrf_merge(
    *ranked_lists: list[int],
    k: int = _RRF_K,
) -> list[tuple[int, float]]:
    """Merge multiple ranked lists via RRF. Returns ``[(idx, score), ...]`` descending."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, idx in enumerate(ranked):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


# ---------------------------------------------------------------------------
# Reranker (optional, lazy-load)
# ---------------------------------------------------------------------------

_DEFAULT_RERANKER = "BAAI/bge-reranker-base"


@dataclass
class _LazyReranker:
    """Lazy-loaded cross-encoder. Failed loads set model=None permanently."""

    model_name: str
    _model: object | None = None
    _tried: bool = False

    def score(self, pairs: list[tuple[str, str]]) -> list[float] | None:
        if self._tried and self._model is None:
            return None
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder

                logger.info("Loading reranker: %s", self.model_name)
                self._model = CrossEncoder(self.model_name)
            except Exception as e:
                logger.warning(
                    "Reranker '%s' load failed (%s); rerank disabled, falling back to BM25+Dense+RRF",
                    self.model_name, e,
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
    """Builds dense + sparse indices once at startup; runs hybrid retrieval at query time.

    Design:
        - Chunks are immutable; index position = chunk list index, aligned across indices.
        - Dense encodes ``title`` (breadcrumb summary), BM25 indexes ``title + body``.
        - ``module_filter`` is a *boost* (1.5x), not a hard filter — wrong module inference
          won't kill results.
    """

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        embed_model_name: str = _DEFAULT_EMBED_MODEL,
        enable_rerank: bool = False,
        reranker_name: str = _DEFAULT_RERANKER,
        cache_dir: Path | None = None,
    ) -> None:
        if not chunks:
            raise ValueError("HybridRetriever requires non-empty chunks list")
        self.chunks: list[Chunk] = list(chunks)
        self._embed_model_name = embed_model_name
        self._cache_dir = cache_dir

        dense_loaded = self._try_load_dense_cache() if cache_dir else False
        if not dense_loaded:
            self._build_dense(embed_model_name)
            if cache_dir:
                self._save_dense_cache()

        self._build_bm25()
        self._reranker = _LazyReranker(reranker_name) if enable_rerank else None

    # -- index build ---------------------------------------------------------

    def _build_dense(self, model_name: str) -> None:
        titles = [c.title or c.section or c.body[:120] for c in self.chunks]
        self._embed_model = SentenceTransformer(model_name)
        embeddings = np.asarray(self._embed_model.encode(titles, convert_to_numpy=True))
        embeddings = embeddings.astype("float32")
        self._dense = faiss.IndexFlatL2(embeddings.shape[1])
        self._dense.add(embeddings)

    def _build_bm25(self) -> None:
        corpus = [_tokenize(f"{c.title}\n{c.body}") for c in self.chunks]
        corpus = [doc or ["__empty__"] for doc in corpus]
        self._bm25 = BM25Okapi(corpus)

    # -- FAISS cache ---------------------------------------------------------

    def _dense_cache_paths(self) -> tuple[Path, Path]:
        assert self._cache_dir is not None
        idx_dir = self._cache_dir / "faiss"
        return idx_dir / "index.faiss", idx_dir / "meta.json"

    def _compute_chunks_hash(self) -> str:
        h = hashlib.sha256()
        for c in self.chunks:
            h.update(c.chunk_id.encode())
            h.update(c.title.encode())
            h.update(c.body.encode())
            h.update(c.module.encode())
            h.update(c.kind.encode())
        h.update(self._embed_model_name.encode())
        return h.hexdigest()

    def _try_load_dense_cache(self) -> bool:
        idx_path, meta_path = self._dense_cache_paths()
        if not idx_path.is_file() or not meta_path.is_file():
            logger.info("FAISS cache not found, rebuilding index")
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            current_hash = self._compute_chunks_hash()
            if meta.get("chunks_hash") != current_hash:
                logger.info(
                    "FAISS cache expired (chunks changed), rebuilding "
                    "(cached=%s... current=%s...)",
                    meta.get("chunks_hash", "")[:16],
                    current_hash[:16],
                )
                return False
            self._dense = faiss.read_index(str(idx_path))
            self._embed_model = SentenceTransformer(meta["embed_model"])
            logger.info(
                "FAISS index loaded from cache: %d vectors, dim=%d, embed=%s",
                meta["num_vectors"], meta["dim"], meta["embed_model"],
            )
            return True
        except Exception as e:
            logger.warning("FAISS cache load failed (%s), rebuilding", e)
            return False

    def _save_dense_cache(self) -> None:
        idx_path, meta_path = self._dense_cache_paths()
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            faiss.write_index(self._dense, str(idx_path))
            meta = {
                "chunks_hash": self._compute_chunks_hash(),
                "num_vectors": self._dense.ntotal,
                "dim": self._dense.d,
                "embed_model": self._embed_model_name,
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("FAISS index cached: %d vectors, dim=%d → %s", meta["num_vectors"], meta["dim"], idx_path)
        except Exception as e:
            logger.warning("FAISS cache save failed (%s), will rebuild on next startup", e)

    # -- query ---------------------------------------------------------------

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
        if n <= 0:
            return []
        if n >= len(scores):
            order = np.argsort(-scores)
        else:
            top_idx = np.argpartition(-scores, n - 1)[:n]
            order = top_idx[np.argsort(-scores[top_idx])]
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
        """Hybrid retrieval: BM25 + FAISS → RRF → optional rerank.

        Args:
            query: Natural language or keyword query.
            top_k: Number of chunks to return.
            candidate_pool: Candidates per method feeding into RRF; also rerank input size.
            module_filter: Boosts chunks from this module (1.5x). Soft boost, not hard filter.
            exclude_chunk_ids: Chunks already matched deterministically (skipped to avoid duplicates).

        Returns:
            Top-k chunks, best first.
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

        topn = merged_idxs[:candidate_pool]
        chosen = self._maybe_rerank(query, topn, top_k)
        return [self.chunks[i] for i in chosen]
