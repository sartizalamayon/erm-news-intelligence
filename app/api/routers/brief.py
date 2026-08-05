"""Daily Risk Insight / brief: draft from approved stories grouped by
section, editable fuel-price line, .docx download. See
frontend-contract.md §3.5 and §9 ("GET /brief/draft", "POST
/brief/draft", "GET /brief/download.docx", "PUT /config/fuel-line").

Stub only. TODO(P3-LLM for draft generation; FE-WIRE for the .docx
download wiring). A .docx-writing dependency (e.g. python-docx) is
deliberately NOT added yet — flag back to ARCH-REPO when this router is
actually built.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/brief", tags=["brief"])
