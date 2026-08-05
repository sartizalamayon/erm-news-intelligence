"""Database layer: Postgres (Supabase, ADR §1/§9.4) schema, migrations,
connection handling, and the encrypted-at-rest storage for admin-entered
LLM provider keys (ADR §9.1).

No ORM (GATE 0: "no ORM ceremony beyond what's needed") — the schema's
source of truth is the plain SQL files in `migrations/`, applied by the
small deterministic runner in `app.db.migrate`. Query access is a
handful of parametrized functions in `app.db.queries`, not a mapped
model layer.

Modules:
    connection  — `get_connection()`, a context-managed psycopg
                  connection (commit/rollback/close), reading
                  `DATABASE_URL` from `app.config.Settings`.
    migrate     — the forward-only migration runner (`run_migrations()`,
                  also runnable as `python -m app.db.migrate`).
    settings    — typed access to the runtime-admin-editable `settings`
                  key/value table (storage ceiling, retention windows,
                  OTP domain) — distinct from `app.config.Settings`,
                  which is env-var process-boot config.
    crypto      — Fernet encryption for provider API keys at rest
                  (ADR §9.1), keyed by `app.config.Settings.secret_key`
                  (an environment variable, never the database).
    queries     — the dedup-insert fix, storage introspection queries,
                  and rollup-table upserts that
                  `app.services.storage`/`app.services.data_transfer`
                  and the migrations' own doc comments reference.

See `/Users/ayo_on/Documents/news-intel-rebuild/docs/storage-policy.md`
for the retention/rollup design these modules implement, with the
measured numbers behind it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["get_connection", "get_dsn", "run_migrations"]


def __getattr__(name: str) -> Any:
    # Lazy re-exports (PEP 562) rather than eager top-level imports: importing
    # app.db.migrate eagerly here causes `python -m app.db.migrate` to warn
    # ("found in sys.modules after import of package... prior to execution")
    # since -m would import the submodule a second time as __main__. Lazy
    # attribute access avoids that while keeping `from app.db import
    # get_connection` etc. working for every other caller.
    if name in ("get_connection", "get_dsn"):
        from app.db.connection import get_connection, get_dsn

        return {"get_connection": get_connection, "get_dsn": get_dsn}[name]
    if name == "run_migrations":
        from app.db.migrate import run_migrations

        return run_migrations
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
