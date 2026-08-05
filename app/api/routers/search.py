"""Full-text search archive across every collected article (headline,
English translation, body), regardless of date/status. See
frontend-contract.md §3.6 and §9 ("GET /search?q=&page=").

Stub only. TODO(FE-WIRE for the endpoint; ARCH-DB for whatever indexing
strategy makes full-text search viable on a 0.5GB free-tier Postgres —
see ADR §4's storage/retention contradiction, which this screen's
"38,412 articles across all dates" scope collides with directly).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/search", tags=["search"])
