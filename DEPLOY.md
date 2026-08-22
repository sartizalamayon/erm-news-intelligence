# Deploying ERM News Intelligence

Everything in this repo is committed, green and locally verified. What
remains is genuinely deployment: a remote, a Render blueprint, two URLs
that only exist once Render has named the services, and one data
bootstrap. This file is the whole list — nothing else is outstanding.

Current state, as verified on this machine:

| Check | State |
|---|---|
| Test suite | 1423 passed, 1 skipped |
| `ruff check app tests` | clean (this is a CI gate — it used to fail) |
| `mypy app` | clean, 149 source files |
| Migrations | 0001–0026 apply cleanly from empty |
| `DATABASE_URL` | **localhost** — nothing here has run against production |
| Git remote | **none** — never pushed, so CI has never run |

---

## 1. Push to a remote

CI (`.github/workflows/ci.yml`) and the daily batch
(`.github/workflows/daily-batch.yml`) are GitHub Actions workflows. They
have never executed, because there is nowhere to execute them.

```bash
gh repo create <owner>/<name> --private --source=. --remote=origin --push
```

Decide `--private` vs `--public` deliberately: the repo contains no
secrets (`.env` is gitignored, every deployment value is a Render or
Actions secret), but it does contain the source and the scoring rubric.

Then add the Actions secrets the daily batch reads — without them the
cron job runs but writes nothing:

```bash
gh secret set DATABASE_URL SECRET_KEY GROQ_API_KEY GEMINI_API_KEY MISTRAL_API_KEY RESEND_API_KEY
```

## 2. Launch the Render blueprint

`render.yaml` declares both services. From the Render dashboard:
**New + → Blueprint**, connect the repo, and fill in every `sync: false`
env var it prompts for (`DATABASE_URL`, `SECRET_KEY`, the provider keys,
`OILPRICEAPI_KEY`).

`OILPRICEAPI_KEY` is optional: without it the Brent card reads
"unconfigured" rather than failing anything. Every other blank value has
a real consequence — see the comments in `render.yaml`.

Migrations need no step of their own. The API's `startCommand` is
`python -m app.db.migrate && uvicorn ...`, so the schema is brought up to
date on every deploy and every free-tier cold start, and a failed
migration stops uvicorn rather than serving traffic against a schema the
code disagrees with.

## 3. Set the two URLs that only exist after step 2

Render names the services; these two values have to match those names or
the frontend talks to nothing.

- **`frontend/js/config.js` → `DEFAULT_PROD_API`** (currently
  `https://erm-backend.onrender.com`). Set it to the API service's real
  public URL. There is no build step, so this constant is the only place
  the frontend learns where its API lives.
- **`render.yaml` → `CORS_ALLOW_ORIGINS`** (currently
  `https://erm-frontend.onrender.com`). Set it to the static site's real
  origin. `app/main.py` fails fast at startup if this is empty in
  production and rejects a literal `*` outright, because the session
  cookie requires credentialed CORS.

Commit both and let `autoDeploy` pick them up.

## 4. Bootstrap the data, once

Migrations create the three new tables empty. Two of them need a
one-time fill; the third fills itself.

```bash
# a) Monthly rollups for every historical month already in `articles`.
#    Idempotent — it recomputes each month from scratch and overwrites.
python -m app.db.rollups backfill
```

This is what makes the long-term boards durable. Without it, months
older than `raw_retention_days` (90) eventually report zero — the
rollups are read for counts precisely so a real risk event does not
decay into a quiet month once its articles are purged. The backfill also
covers the new `rollup_monthly_econ`.

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
empty; the fifteen code-defined entities live in `app/db/watchlist.py`.

---

## Verifying the deploy

```bash
curl -s https://<api-host>/healthz        # liveness  -> {"status":"ok"}
curl -s https://<api-host>/health/ready   # readiness -> {"status":"ok","database":"reachable"}
```

`healthCheckPath` is `/health/ready`, so Render will not consider the
deploy live until the database is genuinely reachable.

Then sign in to the static site and check the four Long-term tabs. Each
should show its own window chip — market panels say twelve months,
news-derived boards say however many days the collector has actually
run. If a board reads "No monitored months yet", the batch has not run
yet; that is the correct display, not a fault.

## Things that will go stale without failing

Worth a calendar reminder rather than a code change:

- The vendored TLS chain for `dsebd.org`
  (`app/services/dsebd_ca_bundle.crt`) — the intermediate expires
  **2036-03-21**, and if the exchange ever fixes its own chain the bundle
  can be deleted entirely.
- Three sentences assert facts about the outside world and are true as
  of August 2026: BTRC has moved the page carrying operator subscriber
  stats; Banglalink and Teletalk are unlisted; no free publisher offers a
  monthly BDT series. All three are in
  `app/api/routers/dashboard_long.py` and `app/services/risklines.py`.
- The score-band thresholds (8 / 6 / 5) exist in three places that must
  agree: the rubric in `app/services/llm/prompts.py`, `_RED_MIN` and
  friends in `app/services/evidence.py`, and `meanColor` in
  `frontend/js/screens/dashboard_long.js`.
