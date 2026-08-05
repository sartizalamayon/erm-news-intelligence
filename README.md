# ERM News Intelligence — Web App

A web application for collecting, filtering, reviewing, and exporting daily
risk-intelligence news. Built on the existing news collector.

This is being built in phases. **Phase 1 (done):** database + web server foundation.

---

## What you need installed first

- **Python 3.10+** (you already have this from the collector)
- That's it. Everything else installs with one command below.

---

## How to run it on your laptop (step by step)

Open a terminal/command prompt and do the following.

### 1. Go into the app folder
```
cd news-intel-app
```

### 2. (Recommended) create a virtual environment
A "virtual environment" keeps this app's libraries separate from the rest of
your system. Optional but tidy.

**Windows:**
```
python -m venv venv
venv\Scripts\activate
```
**Mac/Linux:**
```
python3 -m venv venv
source venv/bin/activate
```

### 3. Install the required libraries
```
pip install -r requirements.txt
```

### 4. Set up the database (one time)
```
cd backend
python database.py
python migrate_to_db.py
```
This creates the database and fills it with your 20 sources and 183 keywords.
It also creates a login — **username: `admin`, password: `changeme`**
(you'll change this later).

### 5. Start the web server
```
uvicorn main:app --reload
```
You'll see a message like `Uvicorn running on http://127.0.0.1:8000`.

### 6. Open it in your browser
Go to: **http://127.0.0.1:8000**

You should see a dark status page showing your source/keyword counts.
That means everything works!

To stop the server, press **Ctrl + C** in the terminal.

---

## What you can look at right now

- **http://127.0.0.1:8000** — status page
- **http://127.0.0.1:8000/api/sources** — your sources as data (JSON)
- **http://127.0.0.1:8000/api/stats** — counts
- **http://127.0.0.1:8000/docs** — interactive API documentation
  (FastAPI generates this automatically — a nice way to see what the
  backend can do)

---

## Project structure

```
news-intel-app/
  backend/
    main.py            <- the web server you run (Phase 1)
    database.py        <- defines the database tables
    migrate_to_db.py   <- one-time: loads sources & keywords into the DB
    auth.py            <- password security
    collector_logic.py <- your existing news collector (the engine)
    dedup.py           <- duplicate detection
  data/
    news.db            <- the database (created by the steps above)
    Insight_Sources.xlsx <- your keyword source file
  requirements.txt     <- list of libraries to install
  README.md            <- this file
```

---

## Using the review workflow (Phase 2)

1. Start the server and open http://127.0.0.1:8000
2. Go to **Review**. If empty, click **Run collection** — it fetches today's
   news from all sources in the background (you'll see progress).
3. Each story shows as a card. When several outlets cover the same story, they
   collapse into one card with a **+N also reported** badge — click **N more ›**
   to expand and see the other outlets.
4. **Approve** or **Reject** the whole story with the buttons on the right.
   Inside an expanded group, **keep**/**drop** act on an individual outlet's
   version (use *keep* to promote a specific version into the export).
5. Click **Export approved** to download an Excel report of approved stories.

Note: dedup (the grouping) needs sentence-transformers installed:
 (downloads a model on first run).

## Coming in later phases

- **Phase 3:** Admin panel — add/edit sources & keywords in the UI
- **Phase 4:** Debug/status dashboard — source health, run history
- **Phase 5:** Login screen, scheduling, deployment hardening

---

## Note on data & deployment

This runs entirely on your machine — nothing is sent anywhere. When you're
ready to deploy on the company server, talk to IT/security first, since this
will hold ERM data and have logins. Avoid putting real ERM data on free
external hosting.

## Optional: enable the local LLM (Ollama — free, private)

The app can use a local LLM to score article relevance and write summaries.
It runs entirely on your machine — no API key, no cost, no data leaves the building.

**Setup (one time):**
1. Install Ollama from https://ollama.com
2. Pull a model:  `ollama pull llama3.1:8b`   (needs ~8GB RAM; smaller: `qwen2.5:3b`)
3. Ollama auto-runs a local server at http://localhost:11434

**Turn it on** by starting the app with the env var set:
- Windows (PowerShell):  `$env:LLM_PROVIDER="ollama"; uvicorn main:app`
- Mac/Linux:  `LLM_PROVIDER="ollama" uvicorn main:app`

Optional env vars: `LLM_MODEL=llama3.1:8b`, `LLM_SUMMARIZE=1` (summaries, slower).

If Ollama isn't running, the app detects it and runs normally without the LLM —
nothing breaks, no articles are dropped. The pipeline shows "LLM off" vs "LLM on"
on the overview page.
