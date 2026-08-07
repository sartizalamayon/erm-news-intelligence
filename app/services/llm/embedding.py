"""The embedding-based near-duplicate pre-clustering seam — deliberately
NOT implemented in this task. See `app/services/llm/__init__.py`'s
module docstring for the full explanation: `backend/dedup.py`'s bge-m3
cosine-similarity pass (both the STEP 1 dedup pre-pass and STEP 3's
`_embed_prefold`) needs an embedding/ML library that is not a declared
dependency (`pyproject.toml` lists none, and this task may not edit
that file — flagged to the owner instead of worked around).

`EmbeddingBackend` is the integration seam a future dependency addition
would plug into. `UnavailableEmbeddingBackend` — the only implementation
shipped here — mirrors legacy's OWN degradation path EXACTLY: legacy's
`stage_dedup()` (`pipeline.py:137-160`) and `_embed_prefold()`
(`collection_service.py:623-661`) both check `dedup.is_available()`
first and fall through to "nothing pre-clustered, LLM/rest of the
pipeline handles everything standalone" when it's False — which is
already a real, documented, production-tested code path (it's what
happens if `sentence-transformers`/bge-m3 simply isn't installed on a
given machine), not a shortcut invented for this task.
"""

from __future__ import annotations

from typing import Protocol


class EmbeddingBackend(Protocol):
    def is_available(self) -> bool: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class UnavailableEmbeddingBackend:
    """Always unavailable. `embed()` is never expected to be called
    (every caller checks `is_available()` first, per legacy's own
    pattern) — it raises rather than returning a fabricated embedding,
    so a caller that skips the availability check fails loudly instead
    of silently producing meaningless vectors.
    """

    def is_available(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "no embedding backend is configured — see app.services.llm module docstring "
            "for what's needed to add one (a declared dependency, out of this task's scope)"
        )


def default_embedding_backend() -> EmbeddingBackend:
    return UnavailableEmbeddingBackend()
