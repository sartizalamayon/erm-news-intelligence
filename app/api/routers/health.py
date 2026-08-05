"""Admin: source-health operational dashboard — healthy/attention tiles,
the 5 pipeline stages with run/re-run controls, per-source status table,
run history. See frontend-contract.md §3.10 and §9
("GET /health/overview", "GET /health/sources", "GET /health/runs",
"POST /pipeline/stages/{key}/run", "POST /pipeline/run-all",
"POST /health/sources/{id}/test", "POST /health/sources/{id}/diagnose").

Stub only. TODO(ADMIN-API for the read endpoints; P1-FETCH for what
"run"/"re-run" actually triggers on a pipeline stage). Note
frontend-contract.md §3.10's flag: the mock's top KPI tiles are
hardcoded literals with zero bindings — they must be computed from
real healthRows/runRows data, not treated as a fixed display.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["admin", "health"])
