# Deploying ERM News Intelligence

Follow this file top to bottom and you get a working deploy. It is
written for someone sitting in front of a Render dashboard with a dozen
blank secret fields, which is the actual situation, so every required
value below says what it is, where to get it, and **what breaks if you
leave it blank** — because most of the ways this deploy fails are
silent. The API starts, `/health/ready` returns 200, Render calls the
deploy live, and something is nonetheless broken in a way nothing
reports.

> **A previous version of this file said "Everything in this repo is
> committed, green and locally verified… this file is the whole list —
> nothing else is outstanding."** That was not true when it was written
> and is not true now. Four deploy blockers have been found and fixed
> since — a dead Gemini model that production would have run, the same
> dead model ids seeded into the database, a blank-mail-key lockout with
> no way back in, and a migration race that crashed the deploy — plus a
> fifth found while rewriting this file (the nightly batch would have
> failed on every run; see §1). All are fixed. The point is that "nothing
> else is outstanding" is a claim this project has not yet earned, and
> the **Known limitations** section at the bottom is now part of the
> document rather than absent from it.

Every `python` below means **this project's virtualenv interpreter**
(`.venv/bin/python`). A bare `python` on the machine this was built on
resolves to Anaconda's and dies at import.

## Current state, verified on this machine

| Check | State |
|---|---|
| Python test suite | **1591 passed, 1 skipped** — `.venv/bin/python -m pytest -q` |
| Frontend test suite | **148 passed** — `node --test "tests/frontend/"*.test.mjs`, and it now runs in CI (it did not until 2026-08-24, and had been red for the life of the long-term dashboard) |
| `ruff check app tests` | clean — a CI gate |
| `mypy app` | clean, 149 source files |
| Migrations | every file in `migrations/` applies cleanly from empty, via `python -m app.db.migrate` |
| CI network behaviour | CI no longer fetches from live news sites; the batch smoke check runs fully offline |
| `DATABASE_URL` | **localhost only** — nothing in this repo has ever run against a production database |
| Git remote | **none** — never pushed, so neither GitHub Actions workflow has ever executed |

The last two rows are the honest headline: everything below §1 is
untested against real infrastructure, because there is no real
infrastructure yet.

---

## 1. Push to a remote, and set the Actions secrets

CI (`.github/workflows/ci.yml`) and the daily batch
(`.github/workflows/daily-batch.yml`) are GitHub Actions workflows with
nowhere to run.

```bash
gh repo create <owner>/<name> --private --source=. --remote=origin --push
```

Choose `--private` vs `--public` deliberately: the repo holds no secrets
(`.env` is gitignored; every deployment value is a Render or Actions
secret), but it does hold the source and the scoring rubric.

Then the Actions secrets. **`gh secret set` takes exactly one name per
invocation** — the single-line form this file used to give
(`gh secret set DATABASE_URL SECRET_KEY GROQ_API_KEY …`) is not a
runnable command; `gh` rejects the extra arguments. Each command below
prompts for the value and does not echo it:

```bash
gh secret set DATABASE_URL      # the Supabase Postgres connection string
gh secret set SECRET_KEY        # the SAME Fernet key you give Render — see §2
gh secret set BREVO_API_KEY     # required: the batch runs APP_ENV=production
gh secret set BREVO_FROM_EMAIL  # required, same reason
gh secret set GROQ_API_KEY      # at least one provider key is required
gh secret set GEMINI_API_KEY
gh secret set MISTRAL_API_KEY
gh secret set RESEND_API_KEY    # optional — inert while EMAIL_PROVIDER=brevo
```

**Why the batch needs mail credentials it never uses.**
`.github/workflows/daily-batch.yml` runs with `APP_ENV=production`, and
`app.batch.run_daily.main()` calls the same
`validate_production_settings` the API does. That check validates the
credentials of whichever transport `EMAIL_PROVIDER` selects, and the
default is Brevo. Until this was fixed, the workflow declared
`RESEND_API_KEY` and nothing else, so every scheduled run would have
died with `ConfigurationError: Missing required production
configuration: BREVO_API_KEY (required because EMAIL_PROVIDER=brevo)`
before fetching a single article — a red X every morning and a product
with no news in it. The workflow now declares `EMAIL_PROVIDER: brevo`
and both Brevo secrets, and
`tests/test_batch_workflow_production_env.py` derives that requirement
from `app/config.py` so the two cannot drift apart again.

`SECRET_KEY` must be **the same value** in Actions and on Render. It
keys the HMAC over stored OTP codes and the Fernet encryption of
admin-entered provider keys; two different values means rows written by
one process cannot be read by the other.

## 2. Launch the Render blueprint

`render.yaml` declares both services. In the Render dashboard:
**New + → Blueprint**, connect the repo, and fill in the `sync: false`
values it prompts for.

### The secrets the API genuinely cannot start without

Leave any of these blank and `app.main.create_app()` raises
`ConfigurationError` at startup; the deploy fails loudly, which is the
intended behaviour and much better than the alternative each one used to
produce.

| Secret | Where it comes from | If it is blank or wrong |
|---|---|---|
| `DATABASE_URL` | Supabase → Project Settings → Database → Connection string | Startup refuses. Nothing works without it. |
| `SECRET_KEY` | generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | Startup refuses if blank **or if it is not a real Fernet key**. Anything else (a password, a UUID, a truncated paste) used to boot cleanly and then 500 the first time an admin saved a provider key, days away from the cause. |
| `BREVO_API_KEY` | Brevo dashboard → SMTP & API → API Keys | Startup refuses. This one was a **permanent, silent lockout**: OTP email is the only way to obtain a password (the seeded admin has none), the console fallback is hard-disabled in production, and the deploy reports healthy. Nobody, including you, could ever have logged in. |
| `BREVO_FROM_EMAIL` | the sender address verified in Brevo | Startup refuses — same lockout. A key with no verified sender cannot send. |
| **one of** `GROQ_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `NVIDIA_API_KEY` | each provider's console | Startup refuses if all four are blank. Supply as many as you have: routing degrades per feature rather than failing, so a second and third key are real resilience, not redundancy. |
| `CORS_ALLOW_ORIGINS` | the static site's real origin — see §3 | Declared in `render.yaml` with a placeholder value. Startup refuses if it is empty in production, and rejects a literal `*` outright (the session cookie requires credentialed CORS, where a wildcard is invalid per spec). |

### The rest, and what each actually costs

| Value | Blank means |
|---|---|
| `OILPRICEAPI_KEY` | The Brent crude card on the long-term dashboard shows an honest "not configured" state instead of a number. Nothing else changes. Free, no credit card: <https://www.oilpriceapi.com/auth/signup> |
| `RESEND_API_KEY`, `RESEND_FROM_EMAIL` | Nothing, while `EMAIL_PROVIDER=brevo`. Resend is kept fully working as the alternative for the day a verified sending domain exists; Resend needs one and `grameenphone.com` does not have one, which is why Brevo is the default. |
| `NVIDIA_API_KEY` | One fewer failover provider. |
| `BREVO_FROM_NAME` | Defaults to `ERM` — the display name on OTP emails. |
| `TRUSTED_PROXY_HOPS` | **Do not treat this as a tuning knob.** `render.yaml` sets it to `1`, which is correct for Render's load balancer with nothing else in front. Raise it above the real number of trusted proxies and you reopen a per-IP OTP rate-limit bypass; drop it to `0` and every client behind Render's proxy shares one rate-limit bucket. Change it only if you put a CDN or WAF in front. |

Migrations need no step of their own. The API's `startCommand` is
`python -m app.db.migrate && uvicorn …`, so the schema is brought up to
date on every deploy and every free-tier cold start, and a failed
migration stops uvicorn rather than serving traffic against a schema the
code disagrees with. Concurrent runs are safe — the runner serializes
behind a Postgres advisory lock, which it did not do until recently, and
without it two simultaneous starts (a deploy plus a wake from idle
sleep, or the nightly batch) crashed each other reliably.

## 3. Set the two URLs that only exist after step 2

Render names the services; these two values have to match those names or
the frontend talks to nothing.

- **`frontend/js/config.js` → `DEFAULT_PROD_API`** (currently
  `https://erm-backend.onrender.com`). Set it to the API service's real
  public URL. There is no build step, so this constant is the only place
  the frontend learns where its API lives.
- **`render.yaml` → `CORS_ALLOW_ORIGINS`** (currently
  `https://erm-frontend.onrender.com`). Set it to the static site's real
  origin, exactly — scheme and host, no trailing slash.

Commit both and let `autoDeploy` pick them up.

These are genuinely cross-**site**, not merely cross-subdomain:
`onrender.com` is on the Public Suffix List, so the browser treats the
two services as unrelated sites. That is why the session cookie is sent
`SameSite=None; Secure`, why CORS must name the exact origin, and why
`*` is refused.

## 4. Bootstrap the data, once

Migrations create the new tables empty. Two need a one-time fill; the
third fills itself.

```bash
# a) Monthly rollups for every historical month already in `articles`.
#    Idempotent — recomputes each month from scratch and overwrites.
python -m app.db.rollups backfill
```

This is what makes the long-term boards durable. Without it, months older
than `raw_retention_days` (90) eventually report zero — the rollups are
read for counts precisely so a real risk event does not decay into a
quiet month once its articles are purged. The backfill also covers
`rollup_monthly_econ`.

```bash
# b) Exchange prices. Optional — the daily batch does this every run.
python -c "
from app.db.connection import get_connection
from app.services import market
with get_connection() as c:
    print(market.refresh(c)); c.commit()"
```

Until it has run once, the Market tab shows its honest empty state
("No market data stored yet") rather than an empty chart.

Nothing needs to be done for `watch_entities` — it is meant to start
empty; the code-defined entities live in `app/db/watchlist.py`.

Both commands need `DATABASE_URL` pointing at the production database in
the environment you run them from.

---

## Verifying the deploy

```bash
curl -s https://<api-host>/healthz        # liveness  -> {"status":"ok"}
curl -s https://<api-host>/health/ready   # readiness -> {"status":"ok","database":"reachable"}
```

`healthCheckPath` is `/health/ready`, so Render will not consider the
deploy live until the database is genuinely reachable. Both probes are
exempt from the general rate limiter, so neither can be answered with a
429 and misread as "unhealthy".

Then, in order:

1. **Sign in.** You cannot, yet — the seeded admin has no password. Use
   the OTP flow: request a code, then set a password with it. If this
   step fails, the mail transport is misconfigured; see the table in §2.
   Nothing else in the product is reachable until this works, so do it
   first.
2. **Run a collection** from the Sources screen, or wait for the 00:00
   UTC (06:00 Dhaka) cron.
3. **Check the four Long-term tabs.** Each should show its own window
   chip — market panels say twelve months, news-derived boards say
   however many days the collector has actually run. "No monitored
   months yet" means the batch has not run yet; that is the correct
   display, not a fault.

### If the API will not start

Read the Render log's first traceback. Every startup refusal is a
`ConfigurationError` naming the exact environment variable and why it is
required — that is what those messages are for. Match it against the
first table in §2 and fill in that one value.

---

## The database locale, before you create it

**Create the Supabase database with `en_US.UTF-8`** (or another full
language locale). `C`, `POSIX` and `C.UTF-8` are all unusable on Linux
and will stop the deploy.

Note the trap in that list: `C.UTF-8` DOES work on macOS and does NOT
work on Linux/glibc, which is where this deploys. That was measured the
wrong way round once already, on a developer machine, and the guard
below is what caught it on the first Linux run. Do not port a locale
choice between platforms without re-checking it.

This is not a preference. Postgres `\y` (word boundary) decides what
counts as a word character from `LC_CTYPE`, roughly half this corpus is
Bangla, and every pattern in `app/db/econaxes.py` and
`app/db/watchlist.py` uses `\y`. Under `C` or `POSIX`, Bangla letters
are not word characters, so those patterns match **nothing** in Bangla
and **nothing raises**. Collection runs, the pipeline completes, the
dashboard renders, and every Bangla-derived number is quietly near
zero, which looks exactly like a quiet news day.

You should not have to remember this: `app/db/collation_guard.py` runs
on every deploy and every cold start, as the first thing
`python -m app.db.migrate` does, and refuses to migrate such a database
at all. Since the start command is
`python -m app.db.migrate && uvicorn ...`, a wrong locale fails the
deploy loudly with the offending `LC_CTYPE` named, instead of serving
traffic against it.

It is worth knowing anyway, because the fix is to **recreate** the
database, not to change a setting: a database's locale is fixed at
creation.

## Known limitations and things that go stale

These are outstanding, deliberately, and none of them blocks a deploy.

- **Two areas never got a pass of their own, and you should know which.**
  The pre-deploy work ran four audits (fabricated data, deployment
  configuration, frontend completeness, and an independent review of the
  changes) plus an edge-case sweep covering hostile input on the API
  surface and every route against an empty database. NOT covered by a
  dedicated pass: **concurrency** (two users deciding the same article,
  a bulk action overlapping a single decision, the reaper firing while a
  run is live) and **failure injection on outbound calls** (timeout,
  TLS failure, 429, redirect loop, HTML where XML was expected). A
  backend-robustness audit aimed at exactly these was started twice and
  died to session limits both times; it was never completed. Individual
  pieces are covered by unit tests and the migration-concurrency suite
  is real, but nobody has gone looking for trouble in those two places.
  The first production incident, if there is one, is likelier to come
  from there than from anywhere else in this document.

- **Nine LLM tuning knobs bypass the settings module.** `LLM_TRANSLATE`,
  `LLM_DEDUP`, `LLM_JUDGE`, `LLM_TOPIC`, `LLM_BATCH`, `LLM_TOPIC_CHUNK`,
  `LLM_TOPIC_DEDUP_BATCH`, `TOPIC_PREFOLD_THRESH` and `GROQ_RETRIES` are
  read straight from `os.environ` inside `app/services/enrichment.py`
  and `app/services/llm/transport.py`, not through
  `app.config.Settings`. So nothing validates them —
  `LLM_BATCH="ten"` is a crash partway through a nightly run, not a
  rejected config — and they are not in `render.yaml`'s env list, so the
  dashboard never offers them. They are documented in `.env.example` and
  in a comment block in `render.yaml`; **migrating them into `Settings`
  is real outstanding work.**
- **The API does not know it is behind TLS.** Render terminates TLS and
  forwards plain HTTP, and uvicorn is deliberately not told to trust
  `X-Forwarded-Proto` — the one flag that would fix the scheme also
  hands `request.client` to the caller and would reopen the per-IP OTP
  rate-limit bypass. Starlette's slash-redirect (the only thing that
  built an absolute URL from that scheme) is turned off instead, so a
  trailing-slash URL returns an honest 404 rather than a redirect the
  browser blocks. No route or client depends on it today.
- **The general rate limiter is per-process.** In-process counters, no
  Redis. On the current single free instance that is the whole story; if
  this is ever scaled past one instance, the limit stops being global.
- **A single Render instance, free plan.** It sleeps after 15 minutes
  idle; the first request after that pays a cold start plus a migration
  check.
- **The vendored TLS chain for `dsebd.org`**
  (`app/services/dsebd_ca_bundle.crt`) — the intermediate expires
  **2036-03-21**. If the exchange ever fixes its own chain, the bundle
  can be deleted entirely.
- **Three sentences assert facts about the outside world**, true as of
  August 2026: BTRC has moved the page carrying operator subscriber
  stats; Banglalink and Teletalk are unlisted; no free publisher offers
  a monthly BDT series. All three are in
  `app/api/routers/dashboard_long.py` and `app/services/risklines.py`.
- **The score-band thresholds (8 / 6 / 5) exist in three places that
  must agree**: the rubric in `app/services/llm/prompts.py`, `_RED_MIN`
  and friends in `app/services/evidence.py`, and `meanColor` in
  `frontend/js/screens/dashboard_long.js`.
