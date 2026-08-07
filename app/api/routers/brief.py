"""Daily Risk Insight / brief: draft from approved stories grouped by
section, editable fuel-price line, .docx download. See
frontend-contract.md §3.5 and frontend/js/screens/brief.js's header
comment for the exact shape this was built against.

Every route requires an authenticated session (`get_current_user`) —
this is a user-facing screen, not an admin one.

Two routers, not one: `PUT /config/fuel-line` is NOT under the `/brief`
prefix in the frontend contract (brief.js calls `api.put("/config/fuel-line",
...)` verbatim) — `config_router` carries that one route and is
registered separately in app.api.routers.ALL_ROUTERS alongside `router`.

`.docx` generation needs `python-docx`, which is NOT an installed
dependency (pyproject.toml is ARCH-REPO's file) — `GET /brief/download.docx`
returns 501 with that flagged explicitly rather than serving a fake file.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import BriefDraftIn, BriefDraftOut, FuelLineIn
from app.db.connection import get_connection
from app.services import brief as brief_service

router = APIRouter(prefix="/brief", tags=["brief"])
config_router = APIRouter(prefix="/config", tags=["brief"])

DHAKA = timezone(timedelta(hours=6))


def _parse_date(value: str | None) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(DHAKA).date()


@router.get("/draft", response_model=BriefDraftOut)
def get_draft(
    date_param: str | None = Query(None, alias="date"), user: AuthenticatedUser = Depends(get_current_user)
) -> BriefDraftOut:
    day = _parse_date(date_param)
    with get_connection() as conn:
        draft = brief_service.load_draft(conn, day)
    if draft is None:
        raise HTTPException(status_code=404, detail="no brief drafted yet for this date")
    return BriefDraftOut(**draft)


@router.post("/draft", response_model=BriefDraftOut)
def post_draft(
    body: BriefDraftIn,
    date_param: str | None = Query(None, alias="date"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> BriefDraftOut:
    day = _parse_date(date_param)
    with get_connection() as conn:
        draft = brief_service.generate_draft(conn, day=day, fuel=body.fuel or None)
    return BriefDraftOut(**draft)


@router.get("/download.docx")
def download_docx(
    date_param: str | None = Query(None, alias="date"), user: AuthenticatedUser = Depends(get_current_user)
) -> None:
    raise HTTPException(
        status_code=501,
        detail=(
            "Brief .docx generation requires the 'python-docx' package, which is not an installed "
            "dependency (pyproject.toml is ARCH-REPO's file — flagged, not added here)."
        ),
    )


@config_router.put("/fuel-line")
def put_fuel_line(body: FuelLineIn, user: AuthenticatedUser = Depends(get_current_user)) -> dict[str, str]:
    with get_connection() as conn:
        brief_service.set_fuel_line(conn, body.fuel)
    return {"fuel": body.fuel}
