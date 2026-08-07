"""The multi-provider LLM layer (ADR §9.1, DoD #1-#5): provider
abstraction, config-driven routing, per-provider quota tracking with
automatic failover, and the frozen P3 prompt/parsing contract —
everything `app.services.enrichment` (the P3 stage orchestrator) needs,
built to be a near drop-in target for ADMIN-API's future config UI.

Module map
----------
  `types`      — `Judgment`, `Summary` (the two dataclasses every
                 provider method returns; field names/types frozen).
  `prompts`    — `FIXED_GUIDE`/`DEFAULT_EXAMPLES` (byte-identical to
                 `backend/llm.py`) plus every prompt-builder function.
  `parsing`    — the parse/clamp/malformed-response-fallback logic for
                 all 10 prompt methods, verbatim per p3-contract.md §3.
  `transport`  — `Transport` protocol (matches
                 `tests/helpers/fake_llm.py`'s `FakeLLMProvider` shape
                 exactly) + real `GroqTransport`/`GeminiTransport`.
  `provider`   — `LLMClient`: prompts + parsing + a `Transport`, the
                 common interface over any provider.
  `quota`      — per-provider-per-day token/request tracking against
                 admin-configured ceilings (never hardcoded constants).
  `routing`    — feature -> provider config (`settings.llm_routing`),
                 the "CONFIG, NOT CODE" surface DoD #1 requires.
  `failover`   — `FailoverTransport`: wraps primary+secondary transports
                 behind the same `Transport` interface, quota-aware.
  `registry`   — `build_client_for_feature()`: joins routing + provider
                 accounts (`provider_configs`, decrypted via
                 `app.db.crypto`) + quota into one ready-to-call
                 `LLMClient` per AI feature.

Known gap, reported rather than worked around: `backend/dedup.py`'s
embedding-based near-duplicate pre-clustering (bge-m3 cosine similarity,
both the STEP 1 dedup pre-pass and STEP 3's `_embed_prefold`) has NO
equivalent here. No embedding/ML library is a declared dependency
(`pyproject.toml` lists none, and this task may not edit that file —
mission: "report any other need to me"). `app.services.enrichment`
degrades exactly the way legacy ITSELF degrades when
`dedup.is_available()` is False (`pipeline.py:146-160`): every article
stays its own standalone group, `stage_llm_verify_groups`'s LLM pass
has nothing to verify (no group ever has 2+ members), and
`_embed_prefold` is skipped, all LLM-fold decisions falling through to
`find_duplicates_batch` alone. This is a faithful, already-documented
legacy fallback path, not an invented one — but it means dedup grouping
quality is currently 100% LLM-driven with no embedding pre-pass, which
was never true in production. Flagged for the owner: adding
`sentence-transformers` (or an equivalent) to `pyproject.toml` is the
fix, scoped to ARCH-REPO per that file's ownership note.
"""

from __future__ import annotations
