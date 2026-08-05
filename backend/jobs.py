"""
jobs.py — run collection + LLM enrichment in the background
============================================================
Two independent background jobs, each with its own progress the UI can poll:

  collection : fetch + keyword filter. Finishes quickly so articles appear for
               review right away.
  llm        : translate → semantic filter → dedup → relevance scoring. Runs
               AFTER collection (or on demand). Updates articles live.

The collection job automatically kicks off the LLM job when it finishes (if a
model is configured), so the user clicks once and both run in sequence.

Cancellation
------------
A shared cancel flag lets a "Force Stop" button halt whatever is running. The
collection and enrichment loops check `is_cancelled()` at safe checkpoints
(between sources, between stages, between score batches) and stop cleanly,
keeping whatever was already written to the DB.
"""

import threading
from collection_service import run_collection, run_llm_enrichment

# ── collection status ─────────────────────────────────────────────────────────
_c = {"running": False, "done": 0, "total": 0, "current": "", "result": None, "error": None}
# ── LLM enrichment status ─────────────────────────────────────────────────────
_l = {"running": False, "done": 0, "total": 0, "step": "", "result": None, "error": None}
# ── shared cancellation flag ──────────────────────────────────────────────────
_cancel = {"flag": False}
_lock = threading.Lock()


def is_cancelled():
    """Checked by the collection/enrichment loops at safe checkpoints."""
    with _lock:
        return _cancel["flag"]


def request_stop():
    """Signal any running job to halt at its next checkpoint. Returns a short
    summary of what was running so the UI can report it."""
    with _lock:
        was = []
        if _c["running"]:
            was.append("collection")
        if _l["running"]:
            was.append("processing")
        _cancel["flag"] = True
    return {"stopping": was}


def _clear_cancel():
    with _lock:
        _cancel["flag"] = False


def _c_progress(done, total, current):
    with _lock:
        _c["done"], _c["total"], _c["current"] = done, total, current


def _l_progress(done, total, step=""):
    with _lock:
        _l["done"], _l["total"], _l["step"] = done, total, step


def _llm_worker(stages=None):
    try:
        result = run_llm_enrichment(progress=_l_progress, stages=stages,
                                    should_stop=is_cancelled)
        with _lock:
            _l["result"], _l["error"] = result, None
    except Exception as e:
        with _lock:
            _l["error"] = str(e)
    finally:
        with _lock:
            _l["running"] = False
            _l["step"] = ""
        _clear_cancel()


def _start_llm(stages=None):
    """Start LLM enrichment in the background. Called automatically after
    collection, or directly via the API."""
    with _lock:
        if _l["running"]:
            return False
        _l.update(running=True, done=0, total=0, step="", result=None, error=None)
    _clear_cancel()
    threading.Thread(target=_llm_worker, args=(stages,), daemon=True).start()
    return True


def _collection_worker(trigger):
    ok = False
    cancelled = False
    try:
        result = run_collection(trigger=trigger, progress=_c_progress,
                                should_stop=is_cancelled)
        with _lock:
            _c["result"], _c["error"] = result, None
        ok = True
        cancelled = bool(result and result.get("cancelled"))
    except Exception as e:
        with _lock:
            _c["error"] = str(e)
    finally:
        with _lock:
            _c["running"] = False
            _c["current"] = ""
    # chain enrichment only if the fetch succeeded AND wasn't cancelled
    if ok and not cancelled and not is_cancelled():
        _start_llm()
    else:
        _clear_cancel()


def start_collection(trigger="manual"):
    """Begin a collection in the background. Returns False if already running."""
    with _lock:
        if _c["running"] or _l["running"]:
            return False
        _c.update(running=True, done=0, total=0, current="starting...",
                  result=None, error=None)
    _clear_cancel()
    threading.Thread(target=_collection_worker, args=(trigger,), daemon=True).start()
    return True


def start_llm(stages=None):
    """Manually (re-)run LLM enrichment, e.g. an 'Analyze' button."""
    return _start_llm(stages)


def get_status():
    """Combined snapshot: collection + LLM progress.

    `active` is the single source of truth for the UI: True whenever ANYTHING
    is running (collection OR enrichment). This is what fixes the vanishing-bar
    desync — the UI keeps the bar up while `active` is true, regardless of which
    of the two phases currently holds the work, so the collection→LLM handoff
    gap never orphans the bar.
    """
    with _lock:
        active = _c["running"] or _l["running"]
        return {
            "active": active,
            "cancelling": _cancel["flag"],
            "running": _c["running"], "done": _c["done"], "total": _c["total"],
            "current": _c["current"], "result": _c["result"], "error": _c["error"],
            "llm": {"running": _l["running"], "done": _l["done"],
                    "total": _l["total"], "step": _l["step"], "error": _l["error"]},
        }
