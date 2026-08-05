"""Admin: AI scoring guidance + few-shot examples, "suggest a
refinement", candidate keywords mined from approve/reject history. See
frontend-contract.md §3.9 and §9 ("GET /prompts/config",
"GET /prompts/candidates", "PUT /prompts/guidance",
"PUT /prompts/examples", "POST /prompts/suggest").

Stub only. TODO(ADMIN-API for CRUD/persistence; P3-LLM for the
"suggest a refinement" action, which frontend-contract.md §3.9 is
explicit "genuinely needs an LLM call server-side, not fakeable"). The
system-prompt text itself is frozen per ADR §3/§9.1 — this router
manages the guidance/example *content* operators edit, not the frozen
prompt scaffold P3-LLM builds around it.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/prompts", tags=["admin", "prompts"])
