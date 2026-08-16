"""Admin: AI scoring guidance + few-shot examples with versioning,
"suggest a refinement", candidate keywords mined from approve/reject
history — AND, bundled into this same router (see below for why),
provider routing/quota config (ADR §9.1, DoD #4).

See frontend-contract.md §3.9 and §9 ("GET /prompts/config",
"GET /prompts/candidates", "PUT /prompts/guidance",
"PUT /prompts/examples", "POST /prompts/suggest"), plus this task's own
DoD #3 (versioning: every save creates a new version, previous versions
are retrievable, the active version is selectable — see
app.services.admin's "AI prompts + versioning" section for the
mechanism) and DoD #4 (routing config).

**The system-prompt text itself (`FIXED_GUIDE`,
app.services.llm.prompts) is a FROZEN behavioral contract per
p3-contract.md — this router versions the ADMIN-EDITABLE guidance/
examples CONTENT, never the frozen scaffold P3-LLM builds around it.**
"suggest a refinement" calls a real LLM (app.services.admin.suggest_refinement
-> app.services.llm.registry.build_client_for_feature ->
LLMClient.suggest_guidance, all P3-LLM-built) — never fabricated.

Why provider routing/quota config lives HERE rather than a dedicated
router file: this task's owned-files list is fixed
(sources.py/keywords.py/prompts.py/dashboard.py/health.py) and does not
include one, and app/api/routers/__init__.py (which owns `ALL_ROUTERS`)
is not in that list either — every other owned router already has an
established theme (source config, keyword config, storage, source
health) that routing/provider/quota config does not fit. This router's
existing theme — the admin-editable LLM feature surface — is the
closest fit, so `/prompts/providers`, `/prompts/routing`, and
`/prompts/quota` live under the existing `/prompts` prefix rather than
matching frontend-contract.md §9's unprefixed proposal literally (that
section is explicitly "a design proposal... not an observed fact").

Every route is gated by `require_admin` (app.api.deps).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.admin import (
    PromptCandidateOut,
    PromptConfigOut,
    PromptSaveBody,
    PromptSuggestOut,
    PromptVersionOut,
    ProviderConfigOut,
    ProviderConfigUpdate,
    ProviderCredentialCreate,
    ProviderCredentialOut,
    ProviderDefinitionOut,
    ProviderDefinitionUpsert,
    ProviderLimitsIn,
    ProviderQuotaOut,
    RoutingConfigIO,
)
from app.db.connection import get_connection
from app.services import admin as admin_service

router = APIRouter(prefix="/prompts", tags=["admin", "prompts"])


# ── Prompt content + versioning ──────────────────────────────────────────


@router.get("/config", response_model=PromptConfigOut)
def get_config(admin: AuthenticatedUser = Depends(require_admin)) -> PromptConfigOut:
    with get_connection() as conn:
        data = admin_service.get_prompt_config(conn)
    return PromptConfigOut(**data)


@router.put("/guidance", response_model=PromptVersionOut)
def save_guidance(body: PromptSaveBody, admin: AuthenticatedUser = Depends(require_admin)) -> PromptVersionOut:
    with get_connection() as conn:
        version = admin_service.save_prompt_content(
            conn, "guidance", body.content, actor_email=admin.email, note=body.note
        )
    return PromptVersionOut(**version)


@router.put("/examples", response_model=PromptVersionOut)
def save_examples(body: PromptSaveBody, admin: AuthenticatedUser = Depends(require_admin)) -> PromptVersionOut:
    with get_connection() as conn:
        version = admin_service.save_prompt_content(
            conn, "examples", body.content, actor_email=admin.email, note=body.note
        )
    return PromptVersionOut(**version)


@router.get("/guidance/versions", response_model=list[PromptVersionOut])
def list_guidance_versions(admin: AuthenticatedUser = Depends(require_admin)) -> list[PromptVersionOut]:
    with get_connection() as conn:
        versions = admin_service.list_prompt_versions(conn, "guidance")
    return [PromptVersionOut(**v) for v in versions]


@router.get("/examples/versions", response_model=list[PromptVersionOut])
def list_examples_versions(admin: AuthenticatedUser = Depends(require_admin)) -> list[PromptVersionOut]:
    with get_connection() as conn:
        versions = admin_service.list_prompt_versions(conn, "examples")
    return [PromptVersionOut(**v) for v in versions]


@router.post("/guidance/versions/{version}/activate", response_model=PromptVersionOut)
def activate_guidance_version(version: int, admin: AuthenticatedUser = Depends(require_admin)) -> PromptVersionOut:
    with get_connection() as conn:
        result = admin_service.activate_prompt_version(conn, "guidance", version)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="guidance version not found")
    return PromptVersionOut(**result)


@router.post("/examples/versions/{version}/activate", response_model=PromptVersionOut)
def activate_examples_version(version: int, admin: AuthenticatedUser = Depends(require_admin)) -> PromptVersionOut:
    with get_connection() as conn:
        result = admin_service.activate_prompt_version(conn, "examples", version)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="examples version not found")
    return PromptVersionOut(**result)


@router.get("/candidates", response_model=list[PromptCandidateOut])
def get_candidates(admin: AuthenticatedUser = Depends(require_admin)) -> list[PromptCandidateOut]:
    with get_connection() as conn:
        candidates = admin_service.mine_keyword_candidates(conn)
    return [PromptCandidateOut(**c) for c in candidates]


@router.post("/suggest", response_model=PromptSuggestOut)
def suggest(admin: AuthenticatedUser = Depends(require_admin)) -> PromptSuggestOut:
    with get_connection() as conn:
        suggestion = admin_service.suggest_refinement(conn)
    return PromptSuggestOut(suggestion=suggestion)


# ── Provider routing config (DoD #4) ─────────────────────────────────────


@router.get("/routing", response_model=RoutingConfigIO)
def get_routing(admin: AuthenticatedUser = Depends(require_admin)) -> RoutingConfigIO:
    with get_connection() as conn:
        routes = admin_service.get_routing_config(conn)
    return RoutingConfigIO(routes=routes)


@router.put("/routing", response_model=RoutingConfigIO)
def put_routing(body: RoutingConfigIO, admin: AuthenticatedUser = Depends(require_admin)) -> RoutingConfigIO:
    with get_connection() as conn:
        routes_in = {feature: route.model_dump() for feature, route in body.routes.items()}
        routes = admin_service.save_routing_config(conn, routes_in)
    return RoutingConfigIO(routes=routes)


# ── Provider accounts: model/role + encrypted key entry (DoD #4) ────────


@router.get("/providers", response_model=list[ProviderConfigOut])
def list_providers(admin: AuthenticatedUser = Depends(require_admin)) -> list[ProviderConfigOut]:
    with get_connection() as conn:
        rows = admin_service.list_provider_configs(conn)
    return [ProviderConfigOut(**row) for row in rows]


@router.patch("/providers/{provider}", response_model=ProviderConfigOut)
def update_provider(
    provider: str, body: ProviderConfigUpdate, admin: AuthenticatedUser = Depends(require_admin)
) -> ProviderConfigOut:
    with get_connection() as conn:
        row = admin_service.upsert_provider_config(
            conn, provider, model=body.model, role=body.role, api_key=body.api_key, updated_by=admin.id
        )
    return ProviderConfigOut(**row)


# ── Quota ceilings + remaining-budget visibility (DoD #4) ───────────────


@router.get("/quota", response_model=list[ProviderQuotaOut])
def get_quota(admin: AuthenticatedUser = Depends(require_admin)) -> list[ProviderQuotaOut]:
    with get_connection() as conn:
        statuses = admin_service.get_quota_status(conn)
    return [ProviderQuotaOut(**s) for s in statuses]


@router.put("/quota/{provider}", response_model=ProviderQuotaOut)
def put_quota_limits(
    provider: str, body: ProviderLimitsIn, admin: AuthenticatedUser = Depends(require_admin)
) -> ProviderQuotaOut:
    with get_connection() as conn:
        status_out = admin_service.save_quota_limit(conn, provider, tpd=body.tpd, rpd=body.rpd)
    return ProviderQuotaOut(**status_out)


# ── Generic provider registry: arbitrary providers + pooled credentials
# (owner's Task 3, app.services.llm.provider_registry) ───────────────────
#
# Distinct from /prompts/providers above (ARCH-DB's provider_configs
# table, permanently fixed to groq/gemini by that table's own CHECK
# constraint): this is the surface that lets an admin add a BRAND-NEW
# provider (Mistral today; anything else later) with its own base_url/
# adapter/model, and manage one or more pooled API keys for it — no
# schema change, ever. Model IDs are always free text here on purpose
# (provider-limits-VERIFIED.md: "two Gemini model IDs are already dead
# to new accounts... model IDs must be runtime config, never constants").


@router.get("/provider-definitions", response_model=list[ProviderDefinitionOut])
def list_provider_definitions(admin: AuthenticatedUser = Depends(require_admin)) -> list[ProviderDefinitionOut]:
    with get_connection() as conn:
        rows = admin_service.list_provider_definitions(conn)
    return [ProviderDefinitionOut(**row) for row in rows]


@router.put("/provider-definitions/{provider}", response_model=ProviderDefinitionOut)
def put_provider_definition(
    provider: str, body: ProviderDefinitionUpsert, admin: AuthenticatedUser = Depends(require_admin)
) -> ProviderDefinitionOut:
    with get_connection() as conn:
        row = admin_service.save_provider_definition(
            conn,
            provider,
            adapter=body.adapter,
            base_url=body.base_url,
            default_model=body.default_model,
            enabled=body.enabled,
            note=body.note,
        )
    return ProviderDefinitionOut(**row)


@router.delete("/provider-definitions/{provider}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider_definition(provider: str, admin: AuthenticatedUser = Depends(require_admin)) -> None:
    """Remove a provider and its stored API keys.

    Refuses (422, via app.errors.ValidationError) when the provider still
    handles a routed feature, or when it is the last one left — see
    `admin_service.delete_provider_definition` for why each of those is a
    silent-breakage trap rather than a matter of taste.
    """
    with get_connection() as conn:
        existed = admin_service.delete_provider_definition(conn, provider)
    if not existed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider not found")


@router.get("/provider-definitions/{provider}/credentials", response_model=list[ProviderCredentialOut])
def list_provider_credentials(
    provider: str, admin: AuthenticatedUser = Depends(require_admin)
) -> list[ProviderCredentialOut]:
    with get_connection() as conn:
        rows = admin_service.list_provider_credentials(conn, provider)
    return [ProviderCredentialOut(**row) for row in rows]


@router.post(
    "/provider-definitions/{provider}/credentials",
    response_model=ProviderCredentialOut,
    status_code=status.HTTP_201_CREATED,
)
def add_provider_credential(
    provider: str, body: ProviderCredentialCreate, admin: AuthenticatedUser = Depends(require_admin)
) -> ProviderCredentialOut:
    with get_connection() as conn:
        row = admin_service.save_provider_credential(conn, provider, body.key_id, body.api_key)
    return ProviderCredentialOut(**row)


@router.delete("/provider-definitions/{provider}/credentials/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider_credential(provider: str, key_id: str, admin: AuthenticatedUser = Depends(require_admin)) -> None:
    with get_connection() as conn:
        admin_service.remove_provider_credential(conn, provider, key_id)
