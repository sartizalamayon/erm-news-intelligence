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

from datetime import date
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


class PromptStagePromptOut(BaseModel):
    """One AI stage's whole system prompt.

    `content` is the active saved version; `effective` is what the model
    is actually sent, which differs only when `fallback_reason` explains
    why the saved one was rejected. `default_content` ships the built-in
    so the console can offer "restore the built-in" without a round trip
    and without keeping a second copy of the text in the frontend.
    """

    field: str
    content: str
    version: int
    effective: str
    default_content: str
    is_default: bool
    fallback_reason: str = ""


class PromptConfigOut(BaseModel):
    guidance: str
    examples: str
    guidance_version: int
    examples_version: int
    approved_count: int
    rejected_count: int
    stages: list[PromptStagePromptOut] = []


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
    # Non-empty when the stored row is loadable but cannot work, e.g. a
    # base_url that already includes the request path. Defaulted so
    # older callers and tests constructing this model still work.
    config_problem: str = ""
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


class StageCallsOut(BaseModel):
    """How many LLM calls a stage made and how many came back — the
    structured form of `app.services.enrichment.StageCalls`.

    Exists as a field rather than only inside `detail` because "every
    call failed" is a fact a UI must be able to render as a state, not
    a sentence it has to parse. A stage whose calls all failed still
    reports the articles it "processed", which is how a wholly dead
    translate stage once rendered as a green success.

    The whole object is null when a stage does not report call
    accounting. That is a real, current state (only the translate stage
    records `StageCalls` today), and it is deliberately distinct from a
    zeroed record: "not counted" must never render as "nothing failed".
    """

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    # One representative error, so a failure count comes with a cause.
    first_error: str = ""


class PipelineStageOut(BaseModel):
    key: str
    name: str
    desc: str
    status: str  # "done" | "not_run" | "error"
    ran_at: str | None
    detail: str = ""
    calls: StageCallsOut | None = None


class PipelineStagePanelOut(PipelineStageOut):
    """`GET /health/pipeline/stages` — a stage row plus the provider and
    model it would use if started right now.

    `route` is `LLMClient.name` as resolved by
    `app.services.llm.registry.build_client_for_feature` — the WHOLE
    failover chain, which with pooled credentials is routinely ten
    candidates long. `route_primary` is its head (who answers unless
    something goes wrong) and `route_fallbacks` how many candidates sit
    behind it, so a label can be short without the full chain being
    lost. All three describe what the stage will ACTUALLY call, not what
    the routing preference says: a feature routed to a provider with no
    usable key resolves to its secondary, and that has happened for real
    on this database.

    Null `route` with a populated `route_error` means nothing is
    configured for the feature, so the UI renders "n/a" with the reason
    instead of a plausible-looking provider name.

    `uses_llm` is false for the one stage that makes no model call at
    all ("Get article text" is a plain HTTP fetch), which is why an
    empty `route` there means "needs none" rather than "not set up".
    """

    feature: str | None = None
    uses_llm: bool = True
    route: str | None = None
    route_primary: str | None = None
    route_fallbacks: int = 0
    route_error: str | None = None


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
    calls: StageCallsOut | None = None


# ── Fetch (P1) trigger + live progress (BUGFIX mission) ─────────────────
# "Run collection" — the real fetch phase this bug report is about, plus
# the P2 keyword-match phase, both real and separate from the 5 LLM
# enrichment stages above. See app.services.admin's "Fetch (P1) trigger"
# / "Match (P2) trigger" module sections for the full reasoning.


class FetchSourceProgressOut(BaseModel):
    """One entry per source that has ALREADY finished, in the order it
    finished — appended live as `app.services.collector.run_collection`'s
    `progress_cb` fires, not a fixed 45-row table rendered up front.
    """

    name: str
    status: str  # "ok" | "empty" | "failed"
    fetched: int
    inserted: int
    duplicate: int
    error: str | None = None


class FetchRunStatusOut(BaseModel):
    """`POST /health/pipeline/fetch/run` and `GET
    /health/pipeline/fetch/status` share this shape — the same live
    snapshot, whether you just started the run or are polling it a
    minute later. `status`: "idle" (nothing has run yet this process)
    | "running" | "done" | "error". `total_sources`/`completed` are the
    real denominator/numerator for a "23/45 done" progress readout;
    `sources` is the real per-source list backing it (see
    FetchSourceProgressOut).
    """

    status: str
    trigger: str | None = None
    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    total_sources: int = 0
    completed: int = 0
    sources: list[FetchSourceProgressOut] = Field(default_factory=list)
    total_fetched: int = 0
    total_inserted: int = 0
    total_duplicate: int = 0
    error: str | None = None


# ── Consolidated run (BUGFIX-NAV mission) ────────────────────────────────
# "Run collection" moved server-side, entirely — see
# app.services.admin's "Consolidated run" module section for why. One
# background thread now runs fetch -> match -> enrich in sequence with
# zero dependency on any HTTP request or browser tab still existing;
# these are the request/response shapes for the single start endpoint
# (`POST /health/pipeline/run`) and the single status endpoint the
# frontend polls purely to RENDER progress (`GET
# /health/pipeline/run/status`) — never to drive what happens next.
#
# Deliberately NOT a re-invention of FetchRunStatusOut/RematchResult/
# StageRunResult above — each phase's shape below mirrors what that
# phase's own existing endpoint already returns, just nested under one
# run instead of requiring three separate polls stitched together
# client-side (which is exactly the stitching that used to live, and
# break, in dashboard.js).


class RunStartBody(BaseModel):
    """`POST /health/pipeline/run` body — the same three phase booleans
    the dashboard's "Run collection" checkboxes have always sent (fetch/
    match/enrich), now consumed server-side to decide the whole run's
    shape instead of only the first phase's.
    """

    fetch: bool = True
    match: bool = True
    enrich: bool = True
    # Which enrichment stages the enrich phase should run. Null (the
    # default, and what every older client sends) means all five,
    # exactly as before. A subset is what lets "re-run just the scoring
    # stage" be the same backgrounded, progress-reporting run as
    # everything else instead of a separate blocking endpoint.
    stages: list[str] | None = None


class ResetDayBody(BaseModel):
    """`POST /health/pipeline/reset-day` body.

    `keep_reviewed` defaults to True so the destructive-by-accident case
    (an admin wanting a clean re-collect) can never take an analyst's
    approved/rejected decisions with it. Setting it False is an explicit
    choice to wipe the whole day.
    """

    day: date | None = None
    keep_reviewed: bool = True


class ResetDayResult(BaseModel):
    """What the reset actually did. `forgotten_urls` is the number that
    matters operationally: it is the count of dedup tombstones dropped,
    i.e. how many URLs became re-collectable again. A reset that deleted
    rows but forgot 0 URLs would mean the day is still un-refetchable —
    which is exactly the silent failure this endpoint exists to prevent.
    """

    deleted: int
    forgotten_urls: int
    day: str


class RunPhasesRequestedOut(BaseModel):
    fetch: bool
    match: bool
    enrich: bool
    # Null means "all five stages"; a list means this run was narrowed
    # to those stages. See RunStartBody.stages.
    stages: list[str] | None = None


class RunFetchPhaseOut(BaseModel):
    """Mirrors FetchRunStatusOut's per-run fields, scoped to just the
    fetch phase of a consolidated run.
    """

    status: str  # "pending" | "running" | "done" | "error"
    detail: str = ""
    sources: list[FetchSourceProgressOut] = Field(default_factory=list)
    total_sources: int = 0
    completed: int = 0
    total_fetched: int = 0
    total_inserted: int = 0
    total_duplicate: int = 0
    run_id: int | None = None
    error: str | None = None


class RunMatchPhaseOut(BaseModel):
    """Mirrors RematchResult, scoped to the match phase of a consolidated
    run, plus a `status`/`detail` for progress rendering.
    """

    status: str  # "pending" | "running" | "done" | "error"
    detail: str = ""
    matched: int = 0
    filtered_out: int = 0
    total: int = 0
    error: str | None = None


class RunStageOut(BaseModel):
    """Mirrors StageRunResult (plus the stage's own name/desc, so the
    frontend doesn't need a second lookup) for one of the 5 enrichment
    stages within a consolidated run.
    """

    key: str
    name: str
    desc: str
    status: str  # "pending" | "running" | "done" | "error"
    detail: str = ""
    # Live progress WHILE the stage runs. Without these a stage showed
    # only "running…", which made a slow stage and a hung stage look
    # identical — the state a genuinely stuck judge stage hid in.
    # `done`/`total` are article counts; `step` is what the stage is
    # doing right now (e.g. "Scoring relevance").
    done: int = 0
    total: int = 0
    step: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    # WHICH provider and model this stage is using, resolved when the
    # stage starts and published live. Same meaning as
    # PipelineStagePanelOut's fields of the same names.
    feature: str | None = None
    uses_llm: bool = True
    route: str | None = None
    route_primary: str | None = None
    route_fallbacks: int = 0
    route_error: str | None = None
    # Populated when the stage finishes. See StageCallsOut.
    calls: StageCallsOut | None = None


class RunEnrichPhaseOut(BaseModel):
    status: str  # "pending" | "running" | "done" | "error"
    stages: list[RunStageOut] = Field(default_factory=list)


class RunStatusOut(BaseModel):
    """`POST /health/pipeline/run` and `GET /health/pipeline/run/status`
    share this shape. `status`: "idle" (nothing started this process) |
    "running" | "done" | "error" (the background thread itself crashed
    outside all per-phase isolation — see
    app.services.admin._run_pipeline_in_background). `phase` is which of
    fetch/match/enrich is currently active ("idle"/"done"/"error" outside
    of a run). Each of `fetch`/`match`/`enrich` is `null` if that phase
    was never requested for the current/most recent run.
    """

    status: str
    phase: str
    trigger: str | None = None
    requested: RunPhasesRequestedOut | None = None
    started_at: str | None = None
    finished_at: str | None = None
    fetch: RunFetchPhaseOut | None = None
    match: RunMatchPhaseOut | None = None
    enrich: RunEnrichPhaseOut | None = None
    error: str | None = None
