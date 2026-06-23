"""RAG module — hybrid retrieval (BM25 + FAISS + RRF fusion).

Install: ``pip install bosch-ai-framework[rag]``

Provides:
    - ``HybridRetriever``: BM25 sparse + FAISS dense + RRF fusion, optional rerank
    - ``ChunkLoader``: load chunks from AST JSONL and markdown files
    - ``Chunk``: immutable chunk dataclass
    - ``LookupIndex``: configurable rule-based first-pass lookup
    - ``LookupHit``: deterministic lookup result

Usage::

    from bosch_ai_framework.rag import HybridRetriever, ChunkLoader, LookupIndex

    # Load
    chunks = ChunkLoader.load_all("docs/")
    lookup = LookupIndex(chunks)
    retriever = HybridRetriever(chunks, cache_dir=Path(".cache"))

    # Deterministic lookup
    hits = lookup.find_from_payload(
        payload={"errorCode": "5013", "processStatus": "ReleaseToFIN"},
        route_url="/billing/api/sd/CA/retrieve",
    )

    # Hybrid search (exclude already-matched chunks)
    results = retriever.retrieve(
        "why is my status ReleaseToFIN?",
        top_k=10,
        exclude_chunk_ids={h.chunk.chunk_id for h in hits},
    )

Extracted from: bosch-bapee
"""

from bosch_ai_framework.rag.corpus import Chunk, ChunkLoader
from bosch_ai_framework.rag.lookup import LookupHit, LookupIndex
from bosch_ai_framework.rag.retriever import HybridRetriever

__all__ = [
    "Chunk",
    "ChunkLoader",
    "HybridRetriever",
    "LookupHit",
    "LookupIndex",
]
