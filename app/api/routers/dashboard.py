"""Dashboard endpoints: short-term (today/14-day) and long-term
(12-month) views. See frontend-contract.md §3.1-3.2 and §9
("GET /dashboard/today", "GET /dashboard/trends?range=14d|12mo").

Stub only. TODO(whoever builds the dashboard — ADR §7.3 flags this as
"the largest net-new block in the project", likely its own agent rather
than folded into FE-WIRE). The 12-month view also depends on ARCH-DB's
rollup/retention design (ADR §4) — do not build long-term trends against
raw rows.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
