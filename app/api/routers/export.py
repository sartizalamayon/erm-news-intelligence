"""Ad-hoc Excel/CSV export builder: story-set/date/section/column/sort
selection, live preview, named presets. See frontend-contract.md §3.4
and §9 ("GET /export/preview", "GET /export/presets", "POST /export",
"POST /export/presets").

Stub only. TODO(FE-WIRE): implement. Needs a spreadsheet-writing
dependency (e.g. openpyxl, as the legacy app already uses) that is
deliberately NOT added to pyproject.toml yet — flag back to ARCH-REPO
when this router is actually built, per this repo's "only ARCH-REPO
edits the manifest" convention.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/export", tags=["export"])
