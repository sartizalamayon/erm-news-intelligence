"""Request/response models for the admin console API (ADMIN-API mission).

Covers every router this task owns: sources, keywords, prompts (+
provider routing/quota, bundled into the prompts router — see that
router's module docstring for why), the storage dashboard, and source
health. Grouped by screen with a `# ── screen ──` header per section,
mirroring frontend-contract.md's own §3.7-§3.10 structure.

All datetimes cross this boundary as ISO-8601 strings, never raw
`datetime`/`date` objects — the service layer (app.services.admin) is
responsible for calling `.isoformat()` before handing a dict back to a
router, so every model below types those fields `str`, not `datetime`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ── Sources (frontend-contract.md §3.7 / §3.10) ─────────────────────────


class SourceOut(BaseModel):
    id: int
    name: str
    methods: list[str]
    language: str
    enabled: bool
    config: dict[str, Any]
    created_at: str | None = None


class SourceCreate(BaseModel):
    name: str
    methods: list[str]
    language: str = "mixed"
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class SourceUpdate(BaseModel):
    name: str | None = None
    methods: list[str] | None = None
    language: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class SourceTestResult(BaseModel):
    """`s.test` / `h.test` — a live, synchronous-feeling probe against
    the registered `app.sources` plugin for this source (frontend-contract.md
    §3.7: `"196 fetched, 11 from today (via sitemap)"`).
    """

    source_id: int
    ok: bool
    fetched: int
    today: int
    method: str
    detail: str


class SourceDiagnoseResult(BaseModel):
    """`h.diagnose` (§3.10) — genuine per-check diagnostics, not the
    mock's fabricated fixed string.
    """

    source_id: int
    reachable: bool
    feed_discovery_ok: bool
    article_url_found: bool
    date_parse_ok: bool
    detail: str


# ── Keywords (frontend-contract.md §3.8) ────────────────────────────────


class KeywordOut(BaseModel):
    """`section` is always a non-empty string in every response — a
    keyword saved with no section comes back as the literal
    "Uncategorized" (app.services.admin._keyword_row_to_dict), never
    `null`. The DB column itself is nullable
    (migrations/0012_keywords_section_nullable.sql); that NULL is
    strictly an internal storage detail this API never surfaces.
    """

    id: int
    section: str
    term_en: str
    term_bn: str
    enabled: bool
    created_at: str | None = None


class KeywordCreate(BaseModel):
    """`section` is optional: omit it, send `null`, `""`, or the literal
    "Uncategorized" to create a keyword with no section assigned (all
    four normalize identically — app.services.admin._normalize_section).
    `term_en`/`term_bn` are independently optional (each may be blank)
    but not BOTH — the service layer rejects that combination.
    """

    section: str | None = None
    term_en: str = ""
    term_bn: str = ""
    enabled: bool = True


class KeywordUpdate(BaseModel):
    section: str | None = None
    term_en: str | None = None
    term_bn: str | None = None
    enabled: bool | None = None


class KeywordSectionOut(BaseModel):
    """One entry per DISTINCT `keywords.section` value currently in use
    (`GROUP BY`, not a dedicated sections table — see
    app.services.admin.list_keyword_sections's docstring for why).
    "Uncategorized" is a real, always-present-when-nonzero entry here,
    standing in for every keyword with no section assigned.
    """

    section: str
    count: int


class KeywordSectionRename(BaseModel):
    """`PATCH /keywords/sections/{section}` body — bulk-renames every
    keyword currently in `{section}` to `name` (or to no section at all
    if `name` is omitted/blank/"Uncategorized"). See
    app.services.admin.rename_keyword_section.
    """

    name: str | None = None


class KeywordImportResultOut(BaseModel):
    """`POST /keywords/import` response — a summary rather than a bare
    200, per task 5: how many rows were newly inserted, how many
    existing rows were updated in place, how many input rows were
    skipped (blank term_en AND term_bn), and any per-row error messages
    (a bad `id` cell, an unexpected exception) that didn't abort the
    rest of the import.
    """

    created: int
    updated: int
    skipped: int
    errors: list[str] = Field(default_factory=list)


class RematchResult(BaseModel):
    """`rematch` — re-runs phase 2 (keyword filter) against today's
    already-fetched, not-yet-decided articles. Never touches rows an
    analyst has already approved/rejected — see
    app.services.admin.rematch_today's docstring.
    """

    matched: int
    filtered_out: int
    total: int


# ── AI prompts (frontend-contract.md §3.9) ──────────────────────────────


class PromptVersionOut(BaseModel):
    version: int
    content: str
    created_at: str
    created_by: str
    note: str = ""
    active: bool


class PromptConfigOut(BaseModel):
    guidance: str
    examples: str
    guidance_version: int
    examples_version: int
    approved_count: int
    rejected_count: int


class PromptSaveBody(BaseModel):
    content: str
    note: str = ""


class PromptCandidateOut(BaseModel):
    """`candidates[]` — mined from real approve/reject decision history
    (app.services.admin.mine_keyword_candidates), not the mock's static
    sample.
    """

    word: str
    lang: str
    approved: int
    rejected: int


class PromptSuggestOut(BaseModel):
    suggestion: str


# ── Provider routing / quota config (ADR §9.1, DoD #4) ──────────────────
# Bundled into the /prompts router — see app/api/routers/prompts.py's
# module docstring for why there is no dedicated router file for this.


class ProviderChoiceIO(BaseModel):
    provider: str
    model: str | None = None


class FeatureRouteIO(BaseModel):
    primary: ProviderChoiceIO
    secondary: ProviderChoiceIO | None = None


class RoutingConfigIO(BaseModel):
    """Keyed by `app.services.llm.routing.FEATURES`: scoring, translation,
    dedup_verification, topic_clustering, summarization.
    """

    routes: dict[str, FeatureRouteIO]


class ProviderConfigOut(BaseModel):
    """Never the plaintext key (ADR §9.1) — `api_key_last4` is the only
    ever-returned trace of a stored key, and `has_key` is a boolean, not
    the ciphertext.
    """

    provider: str
    model: str
    role: str
    api_key_last4: str
    has_key: bool
    updated_at: str | None = None


class ProviderConfigUpdate(BaseModel):
    """`api_key` is write-only: present in the request, never echoed back
    in any response (see ProviderConfigOut). Omit it to leave the
    currently-stored key untouched; pass `""`/omit to change only
    model/role.
    """

    model: str | None = None
    role: str | None = None
    api_key: str | None = None


class ProviderDefinitionOut(BaseModel):
    """A provider's adapter shape (app.services.llm.provider_registry) —
    what kind of API it speaks and where. Distinct from ProviderConfigOut
    (ARCH-DB's `provider_configs` table, fixed to groq/gemini): this is
    the generic surface an admin uses to add ANY provider (e.g. Mistral,
    or one that doesn't exist yet) without a schema change.
    """

    provider: str
    adapter: str  # "openai_compatible" | "gemini"
    base_url: str | None
    default_model: str
    enabled: bool
    note: str = ""


class ProviderDefinitionUpsert(BaseModel):
    adapter: str = "openai_compatible"
    base_url: str | None = None
    default_model: str
    enabled: bool = True
    note: str = ""


class ProviderCredentialOut(BaseModel):
    """One pooled API key for a provider, masked-suffix only — never the
    plaintext (ADR §9.1), same rule as ProviderConfigOut.
    """

    provider: str
    key_id: str
    api_key_last4: str


class ProviderCredentialCreate(BaseModel):
    key_id: str
    api_key: str


class ProviderLimitsIn(BaseModel):
    tpd: int | None = None
    rpd: int | None = None


class ProviderQuotaOut(BaseModel):
    provider: str
    day: str
    tokens_used: int
    requests_used: int
    tpd: int | None
    rpd: int | None
    tokens_remaining: int | None
    requests_remaining: int | None
    tokens_used_pct: float | None
    requests_used_pct: float | None
    exhausted: bool


# ── Storage dashboard (ADR §9.2, DoD #5/#6) ─────────────────────────────


class StorageUsageOut(BaseModel):
    database_size_bytes: int
    ceiling_bytes: int
    pct_used: float
    table_sizes_bytes: dict[str, int]
    daily_growth_bytes: float | None
    projected_days_remaining: float | None


class PurgeRequestBody(BaseModel):
    """`operation` selects which of app.services.storage's three purge
    functions runs: "stale_bodies" | "stale_unmatched" | "matched".
    `confirm=False` (the default) is always a dry run — see that
    module's docstring for the dry-run/confirm contract this mirrors
    exactly, unchanged.
    """

    operation: str
    older_than_days: int | None = None
    older_than: str | None = None  # ISO date — "matched" purge only
    require_exported: bool = True
    confirm: bool = False


class PurgeResultOut(BaseModel):
    operation: str
    confirmed: bool
    rows_affected: int
    detail: str = ""
    # The exact ids previewed (confirmed=False) or actually deleted
    # (confirmed=True) — app.services.storage's RT-6 fix ("Closing RT-6"
    # in that module's docstring) snapshots these server-side so a
    # confirm can never silently exceed what a preceding preview showed.
    row_ids: list[int] = Field(default_factory=list)


class ExportRequestBody(BaseModel):
    day_from: str | None = None
    day_to: str | None = None
    review_status: str | None = None
    mark_exported: bool = True
    path: str | None = None


class ExportResultOut(BaseModel):
    path: str
    articles_exported: int
    bodies_exported: int
    exported_at: str


class ImportRequestBody(BaseModel):
    path: str


class ImportResultOut(BaseModel):
    articles_imported: int
    articles_skipped_existing: int
    bodies_imported: int
    bodies_skipped_existing: int


# ── Source health (frontend-contract.md §3.10) ──────────────────────────


class PipelineStageOut(BaseModel):
    key: str
    name: str
    desc: str
    status: str  # "done" | "not_run" | "error"
    ran_at: str | None
    detail: str = ""


class HealthOverviewOut(BaseModel):
    """The KPI tiles frontend-contract.md §3.10 flags as hardcoded
    literals in the mock ("the only screen... top-of-page KPIs aren't
    wired at all") — computed for real here from `healthRows`/`runRows`.
    """

    healthy: int
    needs_attention: int
    fetched_last_run: int
    matched_last_run: int
    last_run_at: str | None
    stages: list[PipelineStageOut]


class HealthSourceRowOut(BaseModel):
    id: int
    name: str
    method: str
    fetched: int
    kept: int
    status: str  # "healthy" | "quiet" | "error" | "disabled"
    note: str = ""


class RunRowOut(BaseModel):
    id: int
    when: str
    trigger: str
    new_count: int
    matched_count: int
    status: str
    # RT-1-residual (tests/test_redteam_orphaned_runs.py): a hard-killed
    # run leaves status='running' forever between kills and the next
    # batch's app.db.reaper.reap_stale_runs sweep. Surfacing the raw
    # heartbeat plus a computed staleness flag lets an operator (or the
    # Source-health screen) tell that apart from a run genuinely still
    # in progress, instead of trusting `status` alone.
    heartbeat_at: str | None = None
    heartbeat_stale: bool = False


class StageRunResult(BaseModel):
    stage: str
    status: str  # "done" | "error"
    detail: str = ""
