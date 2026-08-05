"""Shared response models.

Kept to a minimal, genuinely-used error envelope — matched to the
handlers registered in app.api.error_handlers.register_exception_handlers.
Anything
router-specific (a dashboard KPI shape, a review-queue article row,
...) belongs in a per-router schema module instead of being invented
here ahead of the real request that would shape it.
"""

from __future__ import annotations

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """Uniform error body returned by every handler in
    app.errors.register_exception_handlers.
    """

    error: str
    detail: str | None = None
