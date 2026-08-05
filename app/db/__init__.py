"""Database layer — intentionally EMPTY.

Schema, migrations, connection/session management, and the encrypted-at-
rest storage for admin-entered LLM provider keys (ADR §9.1) all belong to
ARCH-DB. This package exists only as the reserved location so later
imports (`app.db....`) have a stable home once ARCH-DB lands its brief.

What's already provisioned for ARCH-DB elsewhere in this skeleton, so it
doesn't need a manifest change to get started:
  * `DATABASE_URL` setting — app.config.Settings.database_url (Supabase
    Postgres connection string, ADR §9.4).
  * `psycopg[binary]` — Postgres driver, already in pyproject.toml.
  * `cryptography` — already in pyproject.toml, for the Fernet
    encryption ADR §9.1 requires for stored provider keys.
  * `SECRET_KEY` setting — app.config.Settings.secret_key, intended as
    that Fernet key (ARCH-DB may decide it needs a dedicated
    `ENCRYPTION_KEY` instead; either way that's a config addition, not a
    new dependency).

No ORM is included on purpose (GATE 0: "no ORM ceremony beyond what's
needed"). If ARCH-DB judges one is genuinely required, that's a call for
the project owner, not a default to reach for.
"""
