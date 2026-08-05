"""Review queue: filter/search/sort matched articles; approve/reject/
star/annotate; duplicate-group and topic-cluster expansion; bulk actions.
See frontend-contract.md §3.3 and §9 ("GET /articles", "PATCH
/articles/{id}", "POST /articles/bulk", "POST /articles/{id}/dups/{dupId}/keep|drop").

Stub only. TODO(FE-WIRE): implement. This is the core triage tool and
almost certainly the first screen to wire end-to-end after auth exists.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/articles", tags=["review"])
