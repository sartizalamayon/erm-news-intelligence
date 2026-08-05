"""ERM News Intelligence — rebuilt application package.

This package (`app/`) is the new application, built side-by-side with the
untouched legacy reference implementation in `backend/` (see repo root
README-rebuild.md for the full map). Nothing in here imports from or
writes to `backend/`.

Sub-packages:
    app.config    — typed, env-driven settings (single source of truth).
    app.logging   — structured, redacting logging setup.
    app.errors    — the exception taxonomy shared by every layer.
    app.api       — the FastAPI app: routers, request/response schemas,
                    shared dependencies.
    app.sources   — the source-plugin layer (one file per news outlet
                    plus a registry). Home only — no sources implemented
                    yet; see app/sources/base.py.
    app.batch     — the daily batch CLI entrypoint (`python -m app.batch`),
                    independent of the web app.
    app.services  — business-logic layer shared by API routers and the
                    batch job. Empty — later agents fill it in.
    app.db        — intentionally empty. Schema/migrations/connection
                    management belong to ARCH-DB.
"""
