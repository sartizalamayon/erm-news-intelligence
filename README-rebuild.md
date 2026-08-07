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
                      of the web app; this is what
                      `.github/workflows/daily-batch.yml`'s cron invokes.
                      Runs the real P1 fetch layer (P1-INGEST); LLM
                      enrichment (P3-LLM) lands the same way once wired.
  services/           Business-logic layer shared by routers + batch.
  db/                 Migrations (`migrations/*.sql`), the deterministic
                      runner (`app/db/migrate.py`), connection handling —
                      ARCH-DB's territory.
tests/                pytest suite — config, errors, logging, batch
                      entrypoint, a /healthz smoke test, plus every other
                      agent's own suite (P1/P2/P3/AUTH-OTP/ARCH-DB/...).
.github/workflows/
  ci.yml              Install, lint (ruff), type-check (mypy), test
                      (with a real Postgres service), a batch-entrypoint
                      smoke run — on every push/PR. Never contacts a real
                      LLM provider or the live internet.
  daily-batch.yml     The scheduled production run — see "Deployment"
                      below.
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
python -m app.batch                 # batch job — real fetch, DB write if DATABASE_URL is set
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
- **`resend`** (the SDK) — Resend was approved as the OTP email
  transport (ADR §9.3) and is live (`app/services/email.py`,
  `RESEND_API_KEY`/`RESEND_FROM_EMAIL` in `.env.example`), but it's
  called over its REST API directly via `requests` (already a
  dependency) for what is mechanically one HTTP POST — no SDK needed.
  There is no SMTP anywhere in this system; no `smtp_*` settings exist
  on purpose (see `app/config.py`'s module docstring).
- **A Mistral SDK** — Mistral, Groq, and Cerebras are all
  OpenAI-compatible (`provider-limits-VERIFIED.md`'s "Round 2"), so a
  Mistral integration is expected to reuse `requests` the same way, not
  add a vendor SDK. `MISTRAL_API_KEY`/`MISTRAL_MODEL` are provisioned in
  `app/config.py`/`.env.example`; flag back to `ARCH-REPO` if `P3-LLM`
  concludes a dedicated SDK is genuinely needed.
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
- LLM provider keys (`GROQ_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`)
  here are the **bootstrap/CI/local fallback only** — ADR §9.1 rules
  that admin-entered provider keys are also stored encrypted in the
  database via the future admin dashboard. That storage path is
  `ARCH-DB`/`ADMIN-API`'s to build; `SECRET_KEY` here is provisioned as
  the encryption key for it.

## Deployment — CI vs. the daily production cron

Two workflows, two different jobs:

- **`.github/workflows/ci.yml`** — runs on every push/PR, against a
  disposable CI-local Postgres, with every provider key blank on
  purpose. Never contacts a real LLM provider or the live internet for
  anything business-relevant.
- **`.github/workflows/daily-batch.yml`** — the real scheduled
  production run (ADR §1: GitHub Actions cron as the batch compute
  host). Runs `python -m app.batch` against the real database and real
  provider credentials, once a day, plus on-demand via its
  `workflow_dispatch` trigger.

### Required GitHub Actions secrets

Set these under the repo's **Settings → Secrets and variables →
Actions**. `daily-batch.yml` reads every one of them; none are
committed anywhere, and none belong in `.env.example` with a real
value — that file only ever documents variable *names*, with blank
defaults. Real values for local development go in a real `.env`
(gitignored); real values for production go here, in GitHub's secret
store, never in a file.

| Secret | Used for |
|---|---|
| `DATABASE_URL` | Supabase Postgres connection string (ADR §1/§9.4). Migrations and the batch job's writes both use it. |
| `GROQ_API_KEY` | Groq (primary LLM provider, ADR §9.1). |
| `GEMINI_API_KEY` | Google AI Studio (secondary LLM provider, ADR §9.1). |
| `MISTRAL_API_KEY` | Mistral (third LLM provider — live-verified working, no reasoning-token tax; `provider-limits-VERIFIED.md`'s "Round 2"). |
| `RESEND_API_KEY` | Resend (OTP email transport, ADR §9.3) — `app.config.Settings.resend_api_key`. |
| `SECRET_KEY` | Fernet key encrypting admin-entered provider keys at rest (ADR §9.1) — also required for `validate_production_settings()` to pass, since `daily-batch.yml` runs with `APP_ENV=production`. |

Not a GitHub secret, and shouldn't become one: `RESEND_FROM_EMAIL` (a
verified sender address, not sensitive) is a plain repo variable/env
value if it needs to move out of `.env.example`'s blank default;
`CEREBRAS_API_KEY` was evaluated and is deliberately **not**
provisioned anywhere — every real call against the available account
returns HTTP `payment_required` (no usable free quota), so there is
nothing for that key to configure (`provider-limits-VERIFIED.md`).

### The daily cron: 00:00 UTC (06:00 Asia/Dhaka)

The corpus is Bangladesh-focused and Dhaka has held a fixed UTC+6 offset
since 2009 (no DST — `storage-policy.md`), so the schedule is chosen
against Dhaka local time, not UTC round numbers for their own sake.
Dhaka's calendar day *D* spans UTC `(D-1) 18:00` to UTC `D 17:59`, so it
is only fully over — every source's "today" feed/sitemap/API fully
settled — from UTC `D 18:00` onward. Firing at **00:00 UTC** (Dhaka
06:00, six hours into Dhaka's *next* day) sits comfortably inside that
window with margin for outlets whose own indexing lags slightly past
local midnight, while still finishing well before a typical Bangladesh
business day starts. This mirrors the legacy system's own precedent —
`frontend-contract.md` §3.10 records its last run at Dhaka **06:15**.
See `daily-batch.yml`'s own comment on the `cron:` line for the full
reasoning, including the known GitHub limitation that scheduled runs
can be delayed (rarely dropped) under platform load — `workflow_dispatch`
exists partly to recover from that by hand.

### Timeout: 150 minutes, deliberately below the 360-min Actions cap

Measured collection alone is ~2.5 minutes (confirmed directly during
this task: a real local run against a fresh Postgres took 2m17s for the
full 45-source fetch). LLM enrichment is the long pole and is
rate-limit-paced, not compute-bound — `token-budget.md` §4's own central
estimate is "~73 minutes at an assumed 15 RPM" once Groq's ~200K TPD is
exhausted (in the first ~25-30 calls) and Gemini/Mistral carry the rest.
That estimate's own honest uncertainty (Gemini publishes no rate-limit
headers; its real RPD is unverified, and the pessimistic end of
`token-budget.md`'s range "does not fit" in any bounded time) is why
150 minutes is roughly 2x that central estimate rather than the
estimate itself — generous room for a slower provider day and
jittered retry/backoff, while bounding a single stuck run's cost to
~42% of the hard 360-min ceiling and ~7.5% of the 2,000 free
minutes/month this repo has across every workflow.

### Idempotency — verified, not assumed

A same-day re-run must neither duplicate articles nor corrupt run
history (the fetch layer dedups via `ON CONFLICT DO NOTHING` on indexed
unique constraints — `storage-policy.md` §1 — and P1-INGEST's
`run_collection()` writes/heartbeats/finalizes one `runs` row per
invocation). Verified empirically, not just by reading the code: ran
`python -m app.batch` twice in immediate succession against a real,
freshly-migrated local Postgres. Run 1 fetched 6,091 articles and
inserted 6,090 (1 in-run duplicate). Run 2, seconds later, fetched 6,200
(a few outlets had published more in the interim) and inserted exactly
169 genuinely new ones, correctly identifying the other 6,031 as
duplicates of run 1's rows. Final state: 6,259 total articles, **zero**
duplicate `url_norm` values across the whole table, and two independent
`runs` rows (`id=1`, `id=2`), both cleanly `status='done'` with correct
`total_new` counts and heartbeats — proving a re-run is safe both for
article data and for run history.

### Failure alerting

A silent failure is the worst outcome for an unattended daily job.
`daily-batch.yml` uses two complementary, zero-new-secret mechanisms:

1. **GitHub's own default behavior** for a failed *scheduled* workflow:
   it emails the user who last edited the `cron:` line in the workflow
   file. Zero configuration, but its real-world reliability is bounded
   by that one person's GitHub notification settings, and a single
   email is easy to miss in a normal inbox.
2. **An explicit tracking-issue step**, using the official
   `actions/github-script` action (bundled with every runner,
   authenticated by the automatically-provided `GITHUB_TOKEN` — no new
   secret, no new external service to provision or credential to add).
   On failure it opens (or comments on an already-open) GitHub issue
   labeled `daily-batch-failure`; on the next successful run, a
   matching step closes it with a resolution comment. This leaves a
   persistent, unmissable signal in the repo's own Issues tab rather
   than relying solely on one email, without adding a Slack/Discord/
   PagerDuty webhook or any other new service.

### Stale-run reaper — recommended, not implemented here

`migrations/0002_reference_tables.sql`'s `runs` table has exactly the
columns a watchdog needs (`heartbeat_at`, `idx_runs_status_running`) and
`app/services/collector.py`'s `run_collection()` (P1-INGEST, RT-1) now
writes and heartbeats a `runs` row per invocation — but nothing yet
marks a *past* run stale if the process that owned it was killed before
it could finalize its own row. `daily-batch.yml`'s own
`timeout-minutes: 150` is exactly that failure mode: a SIGKILL from
Actions cannot reliably run Python's `finally` block, so a run killed by
timeout is a `status='running'` row whose `heartbeat_at` simply stops
advancing, forever, unless something else notices — legacy's own
un-reaped equivalent left 16 of 110 runs stuck this way.

Not implemented in this workflow, on purpose: doing it as an `UPDATE
runs SET status='error' WHERE ...` in the YAML would hand-maintain SQL
that duplicates logic `app/services/collector.py` already owns (status
values, the `details` shape, what a normal vs. abnormal finalize looks
like) outside the module responsible for it — the same
hand-maintained-SQL antipattern the migrations step avoids by calling
`app/db/migrate.py` instead of embedding SQL in the workflow.
`app/services/**` and `app/batch/**` are P1-INGEST's active files, not
`ARCH-REPO`'s, so this is a recommendation to route rather than
something reached into and changed here:

**Recommendation:** a small function alongside `_start_run` /
`_touch_heartbeat` / `_finalize_run` in `app/services/collector.py` —
e.g. `reap_stale_runs(conn, stale_after=...)` — marking any
`status='running'` row whose `heartbeat_at` is older than a threshold
(minutes, not this job's 150-minute ceiling) as `status='error'`, called
at the **start** of `run_collection()` / `app.batch.run_daily.main()`.
Every new run then self-heals any previous run's abandoned row before
starting its own — correct by construction on every future invocation,
scheduled or manual, with no separate reaper schedule or workflow step
needed.

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
