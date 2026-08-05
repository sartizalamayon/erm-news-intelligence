# ERM News Intelligence — Rebuild

This is the **new** application, built from scratch alongside the untouched
legacy reference app in `backend/`. It does not import from, write to, or
depend on anything in `backend/` or `data/`.

Full project context (read these before building on top of this skeleton):

- `/Users/ayo_on/Documents/news-intel-rebuild/docs/architecture-decision-record.md`
  — the approved GATE 0 decisions (platform, database, LLM providers, auth,
  storage/retention) and the owner rulings in its §9.
- `/Users/ayo_on/Documents/news-intel-rebuild/docs/speed-techniques.md` §4 —
  the fetch-layer architecture `app/sources/` is shaped to host.
- `/Users/ayo_on/Documents/news-intel-rebuild/docs/frontend-contract.md` §1, §9
  — the 11 screens and ~35 proposed endpoints `app/api/routers/` is shaped
  to host.

This document (and `pyproject.toml`, `.env.example`, `.github/workflows/`)
are owned by `ARCH-REPO`. The existing `README.md` documents the legacy app
and is untouched.

---

## Layout

```
app/
  main.py             FastAPI app factory + ASGI entrypoint (`app = create_app()`)
  config.py           Typed, env-driven Settings (single source of truth)
  logging.py          Structured (JSON-lines) logging, redaction-by-default
  errors.py           Exception taxonomy shared by every layer
  api/
    routers/          One module per screen/functional area — stubs today,
                       each with a TODO naming the agent expected to fill it
    schemas/           Shared + per-router pydantic models
    deps.py            Shared FastAPI dependencies (auth, once AUTH-OTP lands)
    error_handlers.py  Wires app.errors -> HTTP responses
  sources/            Source-plugin layer: NewsSource ABC + registry.
                      One file per outlet lands here later, one per site,
                      per speed-techniques.md §4 point 1. Empty of concrete
                      sources today — that's P1-FETCH's job.
  batch/              Daily batch CLI: `python -m app.batch`. Independent
                      of the web app; this is what GitHub Actions cron
                      invokes. Currently a no-op that proves the shape.
  services/           Business-logic layer shared by routers + batch. Empty.
  db/                 Intentionally EMPTY — schema/migrations/connections
                      are ARCH-DB's territory.
tests/                pytest suite (currently: config, errors, logging,
                      batch entrypoint, and a /healthz smoke test)
.github/workflows/ci.yml   Install, lint (ruff), type-check (mypy), test,
                      and a batch-entrypoint smoke run — on every push/PR.
.env.example          Every setting app.config.Settings reads, documented.
pyproject.toml        The ONE dependency manifest for app/ — see below.
```

`backend/` (legacy, untouched) and `data/` (gitignored, 73MB SQLite corpus,
SELECT-only if read at all) sit alongside `app/` at the repo root.

---

## Running it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in what you need; every default is safe/blank

uvicorn app.main:app --reload      # web app -> http://127.0.0.1:8000/healthz
python -m app.batch                 # batch job (currently a no-op)
pytest                              # test suite
ruff check app tests                # lint
mypy app                            # type-check
```

Render's start command (once deployed) is:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

---

## Dependency manifest: `pyproject.toml`, not `requirements.txt`

The repo root already has a `requirements.txt` belonging to the legacy app
(referenced by the untouched `README.md`'s setup steps) — reusing that
filename for the new app would either collide with it or require touching
a file this task is not allowed to edit. `pyproject.toml` (PEP 621, built
with `hatchling`, a build-time-only tool — nothing extra installed at
runtime) is a clean, separate manifest with no such collision, and
`[tool.hatch.build.targets.wheel] packages = ["app"]` keeps `backend/` out
of the built package entirely.

**Convention for this project: only `ARCH-REPO` edits `pyproject.toml`.**
Later agents that need a new dependency (e.g. `openpyxl` for the Export
screen, a `.docx` writer for the Brief screen, a session/JWT library once
AUTH-OTP picks a mechanism, `resend` once the OTP-email vendor is actually
approved) should flag it back rather than editing this file directly, so
one manifest doesn't get contended edits from parallel agents.

### Every dependency, and why

Runtime:

| Package | One-sentence justification |
|---|---|
| `fastapi` | The web framework — GATE 0 rules lean FastAPI. |
| `uvicorn[standard]` | ASGI server that actually runs the FastAPI app, locally and on Render. |
| `pydantic` | Typed request/response/settings models; FastAPI's own foundation anyway. |
| `pydantic-settings` | Env-var-driven `BaseSettings` — the typed config module DoD #2 requires. |
| `requests` | Sync HTTP client for the source-plugin fetch layer — the ADR's speed plan is explicitly `ThreadPoolExecutor` + `requests`, no asyncio, no async rewrite. |
| `psycopg[binary]` | Postgres driver for the ruled Supabase database (ADR §1/§9.4). Provisioned now, ahead of any code using it, because only `ARCH-REPO` can edit this file and `ARCH-DB` is the very next agent dispatched. |
| `cryptography` | Fernet symmetric encryption for admin-entered LLM provider API keys at rest — an explicit ADR §9.1 security requirement. Provisioned for `ARCH-DB`, same reasoning as `psycopg`. |
| `groq` | Official SDK for the ruled primary LLM provider (ADR §9.1: `qwen/qwen3.6-27b`). Provisioned ahead of `P3-LLM` for the same "only ARCH-REPO edits the manifest" reason. |
| `google-genai` | Official SDK for the ruled secondary LLM provider, Google AI Studio (ADR §9.1). Same reasoning. |

Dev-only (`pip install -e ".[dev]"`):

| Package | One-sentence justification |
|---|---|
| `pytest` | Test runner — the entire point of DoD #5's "run the test suite." |
| `httpx2` | `starlette.testclient`'s current HTTP client — plain `httpx` is deprecated for this use as of the installed `starlette` 1.x, so this avoids a deprecation warning on day one. Needed for in-process FastAPI endpoint tests. |
| `ruff` | One fast linter/formatter replacing flake8+isort+black — CI's "lint" step, minimal footprint. |
| `mypy` | Static type-check against pydantic's typed models — directly supports GATE 2's "zero unhandled exceptions" goal by catching type errors before they become runtime ones. Kept lenient (`ignore_missing_imports = true`) at this stage; tighten later if it earns its keep. |

**Explicitly NOT added, on purpose:**

- **Docker / docker-compose** — rejected at GATE 0 (deploy target is GitHub
  Actions + Supabase + Render; none need it).
- **Celery / Redis** — rejected at GATE 0 ("one producer, one consumer,
  once daily. No back-pressure to arbitrate.").
- **An ORM or migration tool** (SQLAlchemy, Alembic, etc.) — GATE 0: "no ORM
  ceremony beyond what's needed." `app/db/` is reserved but empty; if
  `ARCH-DB` judges an ORM genuinely necessary, that's a call for the
  project owner, not a default.
- **`sentence-transformers` / embeddings** — the legacy semantic-recall
  feature is confirmed dead weight (ADR §6: 4 of 51,886 rows, 0.008%, ever
  promoted) and explicitly due for removal, not porting.
- **`resend`** (or any OTP-email SDK) — the ADR itself flags this as
  "pending owner approval as a new dependency" (§9.3). `.env.example`
  documents generic SMTP settings so `AUTH-OTP` isn't blocked on the
  vendor decision, but no SDK is installed until that's approved.
- **`openpyxl` / a `.docx` writer** — the Export and Brief screens need
  these, but neither router has any implementation yet (Wave 0 builds
  skeleton only); adding the dependency before the code that uses it
  would be guessing at a library choice nobody's made yet. Flagged as a
  TODO in `app/api/routers/export.py` and `brief.py`.
- **A session/JWT library** — the ADR rules OTP-only auth but not the
  session mechanism (signed cookie vs. JWT vs. Supabase's own auth
  primitives are all still open). Left for `AUTH-OTP` to decide and
  request.

---

## Configuration

One typed settings module: `app.config.Settings` (pydantic-settings,
reads `.env` / real env vars, no secrets committed). `.env.example`
documents every field. Two things worth knowing:

- **`validate_production_settings(settings)`** is the fail-fast check
  (DoD #4: "configuration errors fail fast at startup"). It is *not* a
  pydantic validator — those run on every construction, which would
  break tests and partial local configs on purpose. It's called
  explicitly once, from `app.main.create_app()` and
  `app.batch.run_daily.main()`, and only enforces anything when
  `APP_ENV=production`.
- LLM provider keys and SMTP settings here are the **bootstrap/CI/local
  fallback only** — ADR §9.1 rules that admin-entered provider keys are
  also stored encrypted in the database via the future admin dashboard.
  That storage path is `ARCH-DB`/`ADMIN-API`'s to build; `SECRET_KEY`
  here is provisioned as the encryption key for it.

## Logging

`app.logging.configure_logging()` sets up one JSON-lines handler on
stdout — readable in GitHub Actions logs and on Render, parseable by
either. **Redaction is the default path, not opt-in**: any `extra=`
field whose key contains `password`, `api_key`, `token`, `secret`, `otp`,
or `authorization` (case-insensitive) is replaced with `***REDACTED***`
before serialization — see `tests/test_logging.py`. `app.logging.mask()`
is for the one legitimate case that isn't "don't log this at all": ADR
§9.1's requirement to show admins a masked suffix of a stored key.

## Error taxonomy

`app.errors` (see the module docstring for the full design rationale):

- `AppError` — base class.
- `ConfigurationError` — fail-fast at startup, never caught/swallowed.
- `ValidationError` — bad caller input, maps to HTTP 422.
- `SourceError` (`SourceFetchError`, `SourceConfigError`) — per-source
  fetch failures; must be caught once per source in the batch loop so
  one broken outlet never aborts a run. Owner: `P1-FETCH`.
- `ProviderError` (`ProviderRateLimitedError`, `ProviderPermanentError`)
  — external-provider (LLM/SMTP) failures, carrying a `retryable` flag
  callers branch on. Owner: `P3-LLM` / `AUTH-OTP`.

`app.api.error_handlers.register_exception_handlers` wires all of the
above to HTTP responses (422/502/503/500 as appropriate), including a
catch-all `AppError` handler — the concrete mechanism behind GATE 2's
"zero unhandled exceptions across the RED-TEAM suite."

## What's a stub vs. what's real here

Real, working infrastructure: config, logging (with tested redaction),
the error taxonomy, the FastAPI app factory + exception-handler wiring,
the `/healthz` endpoint, the `NewsSource` ABC + registry, the batch CLI
shape, and CI.

Everything else is an intentionally empty or stub placeholder with a
`TODO(<agent>)` comment naming who's expected to fill it in: every
router in `app/api/routers/`, `app/api/deps.py`'s auth dependency,
`app/db/` in its entirety, and every concrete `app/sources/<site>.py`.
No feature/business logic (fetching, keyword matching, LLM calls, auth,
real endpoints) has been implemented — that is out of scope for this
skeleton by design.
