"""
main.py - the web server (FastAPI)
===================================
Run it on your laptop:
    cd backend
    uvicorn main:app --reload
Then open:  http://127.0.0.1:8000
"""

from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from database import get_connection, init_db, sync_sources
from llm import provider_status

BASE = Path(__file__).resolve().parent
app = FastAPI(title="ERM News Intelligence")

# serve the CSS / static files from the static/ folder.
# Create the folder if it's missing so the app never crashes on startup.
STATIC_DIR = BASE / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def on_startup():
    init_db()
    sync_sources()


# ── shared HTML shell ─────────────────────────────────────────────────────────
def page(title: str, body: str, active: str = "") -> str:
    """Wrap page content in the shared layout (top bar + styling)."""
    def nav(href, label, key):
        cls = "active" if key == active else ""
        return f'<a href="{href}" class="{cls}">{label}</a>'
    llm = provider_status()
    llm_pill = ('<span class="pill live"><span class="dot"></span>LLM on</span>'
                if llm["enabled"]
                else '<span class="pill off"><span class="dot"></span>LLM off</span>')
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · ERM News Intelligence</title>
<link rel="stylesheet" href="/static/app.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
</head><body>
<div class="topbar">
  <div class="brand"><span class="mark">ERM</span> News Intelligence</div>
  <nav class="nav">
    {nav('/', 'Dashboard', 'home')}
    {nav('/review', 'Review', 'review')}
    {nav('/report', 'Daily Risk Insight', 'report')}
    {nav('/search', 'Search', 'search')}
    {nav('/sources', 'Sources', 'sources')}
    {nav('/keywords', 'Keywords', 'keywords')}
    {nav('/debug', 'Debug', 'debug')}
    {nav('/keywords/refine', 'Refine', 'refine')}
  </nav>
  <div class="spacer"></div>
  {llm_pill}
  <div class="userchip">Signed in as <b>admin</b></div>
</div>
<div class="wrap">
{body}
</div>
</body></html>"""


# ── overview / home ───────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home():
    from datetime import date
    conn = get_connection()
    today = date.today().isoformat()
    n_sources = conn.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
    krows = conn.execute("SELECT term_en, term_bn FROM keywords WHERE enabled=1").fetchall()
    n_terms = 0
    for r in krows:
        if (r["term_en"] or "").strip():
            n_terms += 1
        if (r["term_bn"] or "").strip():
            n_terms += len([b for b in r["term_bn"].split(",") if b.strip()])
    n_today = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE day=?", (today,)).fetchone()[0]
    n_pending = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE day=? AND review_status='pending'",
        (today,)).fetchone()[0]
    n_approved = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE day=? AND review_status='approved'",
        (today,)).fetchone()[0]
    conn.close()

    llm = provider_status()
    llm_on = llm["enabled"]
    llm_pill = ('<span class="pill live"><span class="dot"></span>active</span>'
                if llm_on else '<span class="pill off"><span class="dot"></span>off</span>')
    llm_note = (llm["name"] if llm_on else "no model configured")

    body = f"""
    <div class="page-head">
      <div class="eyebrow">Daily Risk Intelligence</div>
      <h1>Dashboard</h1>
      <p>Automated news collection, filtering and review for Enterprise Risk
         Management at Grameenphone.</p>
      <hr class="signal-rule">
    </div>

    <div class="stat-grid">
      <div class="stat accent"><div class="idx">01</div><div class="num">{n_sources}</div><div class="lbl">Active sources</div></div>
      <div class="stat"><div class="idx">02</div><div class="num">{n_terms}</div><div class="lbl">Search terms (EN + BN)</div></div>
      <div class="stat"><div class="idx">03</div><div class="num">{n_today}</div><div class="lbl">Collected today</div></div>
      <div class="stat"><div class="idx">04</div><div class="num">{n_approved}</div><div class="lbl">Approved today</div></div>
    </div>

    <div class="panel">
      <h2>How it works</h2>
      <p class="hint">Each day the desk runs this sequence. Configure sources and
         keywords under Sources &amp; Keywords; review and export under Review.</p>
      <table class="dlist">
        <tr><th>Stage</th><th>What it does</th></tr>
        <tr><td class="name">1 · Collect</td><td>Fetches today's news from {n_sources} sources (sitemap, RSS, Google News, API)</td></tr>
        <tr><td class="name">2 · Keyword filter</td><td>Keeps articles matching {n_terms} bilingual search terms</td></tr>
        <tr><td class="name">3 · Dedup</td><td>Groups the same story reported across multiple outlets</td></tr>
        <tr><td class="name">4 · AI relevance</td><td>Scores each article 1–10 for Grameenphone relevance {llm_pill}</td></tr>
        <tr><td class="name">5 · Review &amp; export</td><td>You approve/reject, then export to Excel</td></tr>
      </table>
    </div>

    <div class="panel">
      <h2>AI model</h2>
      <p class="hint">Local LLM via Ollama - runs on this machine, no data leaves.
         {llm_note}. Tune relevance logic under Refine.</p>
    </div>
    """
    return page("Dashboard", body, active="home")


# ── placeholder pages (built in later phases) ─────────────────────────────────
@app.get("/review", response_class=HTMLResponse)
def review_page(sort: str = "date", sources: str = "", colors: str = "", lang: str = "", match: str = ""):
    """The daily review screen. `sort`: date / source / relevance.
    `sources`: comma-separated source names to show (empty = all).
    `colors`: comma-separated colour bands to show - red,yellow,green,magenta
              (empty = all). red=8-10, yellow=6-7, green=5, magenta=<5.
    `lang`: 'en' or 'bn' to filter by source language (empty = all)."""
    from datetime import date
    conn = get_connection()
    today = date.today().isoformat()

    order_by = {
        "date":      "a.published DESC, a.dup_group, a.is_primary DESC",
        "source":    "a.source COLLATE NOCASE, a.dup_group, a.is_primary DESC",
        "relevance": "COALESCE(a.llm_score,0) DESC, a.dup_group, a.is_primary DESC",
    }.get(sort, "a.published DESC, a.dup_group, a.is_primary DESC")

    # build optional filters
    where = ["a.day=?", "a.review_status IN ('pending','approved','rejected')"]
    params = [today]
    source_list = [s for s in sources.split(",") if s.strip()] if sources else []
    if source_list:
        where.append("a.source IN (%s)" % ",".join("?" * len(source_list)))
        params.extend(source_list)
    # colour-band filter: each checked band maps to a score range. An unscored
    # article (llm_score IS NULL) shows only when ALL bands are selected (i.e. no
    # filter), so a partial filter naturally hides not-yet-scored items.
    color_list = [c for c in colors.split(",") if c.strip()] if colors else []
    BANDS = {"red": "COALESCE(a.llm_score,0) >= 8",
             "yellow": "(COALESCE(a.llm_score,0) >= 6 AND COALESCE(a.llm_score,0) < 8)",
             "green": "COALESCE(a.llm_score,0) = 5",
             "magenta": "(a.llm_score IS NOT NULL AND a.llm_score < 5)"}
    if color_list and len(color_list) < 4:
        clauses = [BANDS[c] for c in color_list if c in BANDS]
        if clauses:
            where.append("(" + " OR ".join(clauses) + ")")

    # match-type filter:
    #   'keyword'  → only keyword matches (Pool 2-KW)
    #   'semantic' → keyword UNION semantic (Pool 2-SEM) - the superset view
    # (empty = show everything, unaffected)
    if match == "keyword":
        where.append("a.match_type IN ('keyword','both')")
    elif match == "semantic":
        where.append("a.match_type='semantic'")
    else:
        # "All" = the matched pool: keyword OR semantic (excludes anything
        # that somehow has no match_type)
        where.append("a.match_type IN ('keyword','semantic','both')")

    rows_all = conn.execute(
        f"""SELECT a.id, a.url, a.source, a.title, a.title_en, a.published, a.sections,
                  a.keywords, a.dup_group, a.is_primary, a.also_in, a.review_status,
                  a.llm_score, a.llm_reason, a.summary, a.content_text,
                  a.match_type, a.semantic_score, a.topic_tag, a.topic_standout, a.topic_parent_id,
                  COALESCE(s.language,'') AS language
           FROM articles a
           LEFT JOIN sources s ON s.name = a.source
           WHERE {" AND ".join(where)}
           ORDER BY {order_by}""",
        params
    ).fetchall()

    # language filter: detect Bengali by script in the title (reliable across
    # 'mixed' sources). lang='bn' → titles with Bengali chars; 'en' → without.
    def _has_bengali(t):
        return any('\u0980' <= ch <= '\u09FF' for ch in (t or ""))
    if lang == "bn":
        rows = [r for r in rows_all if _has_bengali(r["title"])]
    elif lang == "en":
        rows = [r for r in rows_all if not _has_bengali(r["title"])]
    else:
        rows = rows_all

    # all source names (for the filter dropdown)
    all_srcs = conn.execute(
        "SELECT DISTINCT source FROM articles WHERE day=? ORDER BY source", (today,)
    ).fetchall()
    conn.close()

    groups = {}
    for r in rows:
        g = r["dup_group"]
        key = f"solo-{r['id']}" if g is None else f"g-{g}"
        groups.setdefault(key, []).append(r)

    n_pending = sum(1 for r in rows if r["review_status"] == "pending")
    n_approved = sum(1 for r in rows if r["review_status"] == "approved")
    # mark the active sort option as selected
    sel_date      = "selected" if sort == "date" else ""
    sel_source    = "selected" if sort == "source" else ""
    sel_relevance = "selected" if sort == "relevance" else ""
    sel_lang_all = "selected" if lang == "" else ""
    sel_lang_en  = "selected" if lang == "en" else ""
    sel_lang_bn  = "selected" if lang == "bn" else ""
    sel_match_all = "selected" if match == "" else ""
    sel_match_kw  = "selected" if match == "keyword" else ""
    sel_match_sem = "selected" if match == "semantic" else ""
    # source filter checkboxes (which are currently active)
    active_srcs = set(source_list)
    src_checkboxes = ""
    for s in all_srcs:
        nm = s["source"]
        checked = "checked" if (not active_srcs or nm in active_srcs) else ""
        src_checkboxes += (f'<label class="src-check"><input type="checkbox" '
                           f'value="{nm}" {checked}> {nm}</label>')
    # relevance filter options
    # colour filter checkbox state (all checked = show all)
    _active = set(color_list) if color_list else {"red", "yellow", "green", "magenta"}
    def _ck(c):
        return "checked" if c in _active else ""

    if not rows:
        body = f"""
        <div class="page-head"><div class="eyebrow">Daily Review</div>
        <h1>Review</h1><p>No articles collected for today yet. Run a collection
        to pull today's news, then review it here.</p><hr class="signal-rule"></div>
        <div class="panel" style="text-align:center; padding:48px 28px;">
          <h2>Nothing to review yet</h2>
          <p class="hint">Click below to fetch today's news from all enabled sources.</p>
          <button class="btn btn-primary" onclick="runCollection()" id="runBtn"
            style="margin-top:8px;">Run collection</button>
          <div class="progress-row" style="margin-top:20px;">
            <div id="progress" class="progress-line"></div>
            <button id="forceStopBtn" class="btn-force-stop" onclick="forceStop()"
                    style="display:none;">⏹ Force Stop</button>
          </div>
        </div>
        {_review_script()}"""
        return page("Review", body, active="review")

    import re as _re
    def _clean(t):
        """Strip any HTML from a stored title so it never renders as raw markup
        (backstop for older articles fetched before titles were cleaned)."""
        t = t or ""
        if "<" in t and ">" in t:
            t = _re.sub(r"<[^>]+>", "", t)
        return t.strip()

    cards_html = ""
    card_records = []   # one per event (primary): (topic_tag, score, published, html)
    for key, items in groups.items():
        primary = next((x for x in items if x["is_primary"]), items[0])
        others = [x for x in items if x["id"] != primary["id"]]
        n_others = len(others)
        status = primary["review_status"]
        status_cls = {"approved": "is-approved", "rejected": "is-rejected"}.get(status, "")
        also_badge = (f'<span class="also-badge">+{n_others} also reported</span>'
                      if n_others else "")
        expand_btn = (f'<button class="expand-btn" onclick="toggleGroup(\'{key}\')">'
                      f'{n_others} more ›</button>' if n_others else "")
        # LLM relevance badge - colour-coded (score stays under the hood):
        #   red 8-10 (most), yellow 6-7, green 5, magenta <5 (least)
        rel = primary["llm_score"]
        rel_badge = ""
        if rel is not None:
            if rel >= 8:
                tier, label = "red", "High"
            elif rel >= 6:
                tier, label = "yellow", "Medium"
            elif rel >= 5:
                tier, label = "green", "Low"
            else:
                tier, label = "magenta", "Least"
            reason = (primary["llm_reason"] or "").replace('"', "'")
            rel_badge = (f'<span class="rel-badge rel-{tier}" '
                         f'title="Relevance {int(rel)}/10 - {reason}">'
                         f'<span class="rel-dot"></span>{label}</span>')
        detail_rows = ""
        for o in others:
            detail_rows += f"""
            <div class="dup-row">
              <div class="dup-src">{o['source']}</div>
              <a class="dup-title" href="{o['url']}" target="_blank">{_clean(o['title'])}</a>
              <div class="dup-actions">
                <button class="mini approve" onclick="decideSub({o['id']},'approved',this)" title="promote this version into the export">keep</button>
                <button class="mini reject" onclick="decideSub({o['id']},'rejected',this)">drop</button>
              </div>
            </div>"""
        # article body toggle - shows fetched content_text inline
        body_text = (primary["content_text"] or "").strip()
        if body_text:
            import html as _html
            body_esc = _html.escape(body_text)
            body_btn = (f'<button class="body-btn" onclick="toggleBody(\'{key}\')" '
                        f'id="bodybtn-{key}">'
                        f'<span class="body-arrow">›</span> Read article</button>')
            body_panel = (f'<div class="body-panel" id="body-{key}" style="display:none;">'
                          f'<div class="body-text">{body_esc}</div></div>')
        else:
            body_btn = ('<span class="body-btn body-btn-empty" title="Body text was not '
                        'retrieved for this article">'
                        '<span class="body-arrow-none">-</span> No article text</span>')
            body_panel = ""
        # match-path badge: semantic-only matches are flagged so analysts and the
        # accuracy test can tell which pool caught the article.
        sem_badge = ""
        if primary["match_type"] == "semantic":
            sc = primary["semantic_score"]
            sc_txt = f"{sc:.2f}" if sc is not None else ""
            sem_badge = (f'<span class="sem-chip" title="Caught by semantic '
                         f'similarity, not a keyword - score {sc_txt}">≈ semantic</span>')
        _card_html = f"""
        <div class="rcard {status_cls}" id="card-{key}" data-status="{status}">
          <div class="rcard-main">
            <div class="rcard-meta">
              <span class="src-chip">{primary['source']}</span>
              {rel_badge}
              <span class="sec-chip">{primary['sections']}</span>
              {sem_badge}
              {also_badge}
              <span class="status-stamp approved">✓ Approved</span>
              <span class="status-stamp rejected">Rejected</span>
              <span class="kw-chip">{primary['keywords']}</span>
            </div>
            <a class="rcard-title" href="{primary['url']}" target="_blank">{_clean(primary['title'])}</a>
            {f'<div class="rcard-title-en">{_clean(primary["title_en"])}</div>' if primary["title_en"] and _has_bengali(primary["title"]) and primary["title_en"] != primary["title"] else ''}
            <div class="rcard-foot">
              <span class="pub">{primary['published']}</span>
              {body_btn}
              {expand_btn}
            </div>
            {body_panel}
          </div>
          <div class="rcard-actions">
            <button class="act approve" onclick="decide({primary['id']},'approved','{key}')">Approve</button>
            <button class="act reject" onclick="decide({primary['id']},'rejected','{key}')">Reject</button>
          </div>
          <div class="dup-detail" id="detail-{key}" style="display:none;">
            {detail_rows}
          </div>
          {{TOPIC_SLOT_{primary['id']}}}
        </div>"""
        card_records.append({
            "id": primary["id"],
            "topic": (primary["topic_tag"] or "").strip(),
            "standout": bool(primary["topic_standout"]),
            "parent": primary["topic_parent_id"],
            "score": primary["llm_score"] if primary["llm_score"] is not None else -1,
            "source": primary["source"],
            "title": _clean(primary["title"]),
            "url": primary["url"],
            "published": primary["published"],
            "content_text": primary["content_text"] or "",
            "html": _card_html,
        })

    # ── assemble the visible list ──
    # Under RELEVANCE sort, fold each topic's non-substantial articles INTO a
    # substantial card's "+N similar topic" dropdown (same pattern as dedup's
    # "+N also reported", one level up):
    #   - SUBSTANTIAL articles (topic_standout=1) render as their own top-level
    #     card; a topic can have several (e.g. 'Hormuz closes' + 'talks end').
    #   - FOLDED articles (topic_standout=0, topic_parent_id set) are removed
    #     from the top level and listed inside their parent card's dropdown.
    #   - Untagged / lone (no topic, or standalone) render as normal cards.
    # Every card html carries a {{TOPIC_SLOT_<id>}} placeholder; we fill it with
    # the dropdown for that card's folded children, or blank it out.
    # Date/source sorts stay flat (no folding) - folding only makes sense when
    # ranking by relevance.
    def _blank_slots(html):
        import re as _r
        return _r.sub(r"\{TOPIC_SLOT_\d+\}", "", html)

    if sort == "relevance":
        by_id = {r["id"]: r for r in card_records}
        # children grouped by their parent id (only valid folds)
        children = {}
        folded_ids = set()
        for r in card_records:
            pid = r["parent"]
            if (not r["standout"]) and pid is not None and pid in by_id:
                children.setdefault(pid, []).append(r)
                folded_ids.add(r["id"])

        rendered = []   # (sort_score, html)
        for r in card_records:
            if r["id"] in folded_ids:
                continue   # rendered inside its parent's dropdown, not top-level
            kids = children.get(r["id"], [])
            if kids:
                kids.sort(key=lambda k: k["score"], reverse=True)
                tkey = f"topic{r['id']}"
                # dropdown rows: compact source + headline + approve/reject,
                # mirroring dedup's "also reported" detail rows
                rows = []
                import html as _htmlmod
                for k in kids:
                    kkey = f"trow{k['id']}"
                    kbody = (k.get("content_text") or "").strip()
                    if kbody:
                        body_ctrl = (
                            f'<button class="body-btn" onclick="toggleBody(\'{kkey}\')" '
                            f'id="bodybtn-{kkey}">'
                            f'<span class="body-arrow">›</span> Read article</button>')
                        body_pnl = (
                            f'<div class="body-panel" id="body-{kkey}" style="display:none;">'
                            f'<div class="body-text">{_htmlmod.escape(kbody)}</div></div>')
                    else:
                        body_ctrl = ('<span class="body-btn body-btn-empty">'
                                     '<span class="body-arrow-none">-</span> No article text</span>')
                        body_pnl = ""
                    # relevance badge (same tiers as the main cards)
                    ks = k["score"]
                    krb = ""
                    if ks is not None and ks >= 0:
                        if ks >= 8:   ktier, klabel = "red", "High"
                        elif ks >= 6: ktier, klabel = "yellow", "Medium"
                        elif ks >= 5: ktier, klabel = "green", "Low"
                        else:         ktier, klabel = "magenta", "Least"
                        krb = (f'<span class="rel-badge rel-{ktier}" '
                               f'title="Relevance {int(ks)}/10">'
                               f'<span class="rel-dot"></span>{klabel}</span>')
                    rows.append(
                        f'<div class="topic-row">'
                        f'<div class="topic-row-main">'
                        f'<div class="topic-row-head">'
                        f'<span class="src-chip">{k["source"]}</span>'
                        f'{krb}'
                        f'</div>'
                        f'<a class="topic-row-title" href="{k["url"]}" target="_blank">{k["title"]}</a>'
                        f'<div class="topic-row-foot">'
                        f'<span class="pub">{k["published"]}</span>'
                        f'{body_ctrl}'
                        f'</div>'
                        f'{body_pnl}'
                        f'</div>'
                        f'<div class="topic-row-actions">'
                        f'<button class="act approve sm" onclick="decide({k["id"]},\'approved\',\'{kkey}\')">Approve</button>'
                        f'<button class="act reject sm" onclick="decide({k["id"]},\'rejected\',\'{kkey}\')">Reject</button>'
                        f'</div></div>')
                topic_label = r["topic"]
                slot = (
                    f'<div class="topic-more-row">'
                    f'<button class="topic-more" onclick="toggleTopic(\'{tkey}\')" '
                    f'id="topicbtn-{tkey}">'
                    f'+{len(kids)} similar on this topic '
                    f'<span class="topic-label">· {topic_label}</span> '
                    f'<span class="topic-arrow">›</span></button>'
                    f'<div class="topic-hidden" id="topic-{tkey}" style="display:none;">'
                    f'{"".join(rows)}</div>'
                    f'</div>')
                html = r["html"].replace(f"{{TOPIC_SLOT_{r['id']}}}", slot)
            else:
                html = _blank_slots(r["html"])
            rendered.append((r["score"], html))

        rendered.sort(key=lambda x: x[0], reverse=True)
        cards_html = "".join(h for _s, h in rendered)
    else:
        cards_html = "".join(_blank_slots(rec["html"]) for rec in card_records)

    body = f"""
    <div class="page-head">
      <div class="eyebrow">Daily Review · {today}</div>
      <h1>Review</h1>
      <p>One card per story. Duplicates from other outlets are folded in -
         expand any card to see them, and approve the group or an individual version.</p>
      <hr class="signal-rule">
    </div>
    <div class="review-bar">
      <div class="rb-stats">
        <span class="rb-stat"><b id="cnt-pending">{n_pending}</b> pending</span>
        <span class="rb-stat"><b id="cnt-approved">{n_approved}</b> approved</span>
      </div>
      <div class="rb-actions">
        <div class="sort-wrap">
          <label for="sortSel">Sort</label>
          <select id="sortSel" onchange="changeSort(this.value)">
            <option value="date" {sel_date}>Date (newest)</option>
            <option value="source" {sel_source}>Source (A–Z)</option>
            <option value="relevance" {sel_relevance}>GP relevance</option>
          </select>
        </div>
        <div class="filter-wrap">
          <button class="btn btn-ghost" onclick="toggleRelFilter()" id="relFilterBtn">Relevance ▾</button>
          <div class="src-dropdown" id="relDropdown" style="display:none;">
            <div class="src-dropdown-head">
              <button class="mini" onclick="setAllRel(true)">All</button>
              <button class="mini" onclick="setAllRel(false)">None</button>
              <button class="mini approve" onclick="applyRelFilter()">Apply</button>
            </div>
            <div class="src-check-list">
              <label class="cf cf-red"><input type="checkbox" id="cf_red" {_ck('red')}><span class="cf-dot"></span>Red (8&ndash;10)</label>
              <label class="cf cf-yellow"><input type="checkbox" id="cf_yellow" {_ck('yellow')}><span class="cf-dot"></span>Yellow (6&ndash;7)</label>
              <label class="cf cf-green"><input type="checkbox" id="cf_green" {_ck('green')}><span class="cf-dot"></span>Green (5)</label>
              <label class="cf cf-magenta"><input type="checkbox" id="cf_magenta" {_ck('magenta')}><span class="cf-dot"></span>Magenta (&lt;5)</label>
            </div>
          </div>
        </div>
        <div class="sort-wrap">
          <label for="langSel">Lang</label>
          <select id="langSel" onchange="changeLang(this.value)">
            <option value="" {sel_lang_all}>All</option>
            <option value="en" {sel_lang_en}>English</option>
            <option value="bn" {sel_lang_bn}>বাংলা</option>
          </select>
        </div>
        <div class="sort-wrap">
          <label for="matchSel">Match</label>
          <select id="matchSel" onchange="changeMatch(this.value)">
            <option value="" {sel_match_all}>All</option>
            <option value="keyword" {sel_match_kw}>Keyword</option>
            <option value="semantic" {sel_match_sem}>Semantic</option>
          </select>
        </div>
        <div class="filter-wrap">
          <button class="btn btn-ghost" onclick="toggleSrcFilter()" id="srcFilterBtn">Sources ▾</button>
          <div class="src-dropdown" id="srcDropdown" style="display:none;">
            <div class="src-dropdown-head">
              <button class="mini" onclick="setAllSrc(true)">All</button>
              <button class="mini" onclick="setAllSrc(false)">None</button>
              <button class="mini approve" onclick="applySrcFilter()">Apply</button>
            </div>
            <div class="src-check-list">{src_checkboxes}</div>
          </div>
        </div>
        <button class="toggle-decided" id="toggleDecidedBtn" onclick="toggleDecided()">Show decided</button>
        <div class="filter-wrap">
          <button class="btn btn-pipeline" onclick="togglePipeline()" id="pipelineBtn">⚙ Processing ▾</button>
          <div class="pipe-dropdown" id="pipeDropdown" style="display:none;">
            <div class="pipe-head">Run a stage</div>
            <button class="pipe-stage" onclick="runStage('translate')">
              <span class="pipe-ico pipe-ico-1">1</span>
              <span class="pipe-txt"><b>Translate</b><small>Bengali → English (title_en)</small></span>
            </button>
            <button class="pipe-stage" onclick="runStage('dedup')">
              <span class="pipe-ico pipe-ico-2">2</span>
              <span class="pipe-txt"><b>Group duplicates</b><small>Near-exact + embeddings + verify</small></span>
            </button>
            <button class="pipe-stage" onclick="runStage('judge')">
              <span class="pipe-ico pipe-ico-3">3</span>
              <span class="pipe-txt"><b>Score relevance</b><small>Colour badges (1–10)</small></span>
            </button>
            <button class="pipe-stage" onclick="runStage('bodies')">
              <span class="pipe-ico pipe-ico-4">⬇</span>
              <span class="pipe-txt"><b>Get Article Text</b><small>Fetch body text for matched articles</small></span>
            </button>
            <button class="pipe-stage" onclick="runStage('semantic')">
              <span class="pipe-ico pipe-ico-5">≈</span>
              <span class="pipe-txt"><b>Semantic filter</b><small>Recover topic matches keywords missed</small></span>
            </button>
            <button class="pipe-stage pipe-stage-topic" onclick="runStage('topic')">
              <span class="pipe-ico pipe-ico-topic">◆</span>
              <span class="pipe-txt"><b>Group topics</b><small>Cluster by theme + flag standout events</small></span>
            </button>
            <div class="pipe-sep"></div>
            <button class="pipe-stage pipe-stage-all" onclick="runStage('')">
              <span class="pipe-ico pipe-ico-all">▶</span>
              <span class="pipe-txt"><b>Run all</b><small>Translate → group → score → topics</small></span>
            </button>
            <div class="pipe-sep"></div>
            <button class="pipe-stage pipe-stage-collect" onclick="runCollection();closePipeline();">
              <span class="pipe-ico pipe-ico-collect">↻</span>
              <span class="pipe-txt"><b>Re-run collection</b><small>Fetch fresh from all sources</small></span>
            </button>
          </div>
        </div>
        <button class="btn btn-ghost" onclick="clearArticles()" id="clearBtn">Clear</button>
        <a class="btn btn-primary" href="/export" id="exportBtn">Export</a>
      </div>
    </div>
    <div class="progress-row">
      <div id="progress" class="progress-line"></div>
      <button id="forceStopBtn" class="btn-force-stop" onclick="forceStop()"
              style="display:none;">⏹ Force Stop</button>
    </div>
    <div class="review-list">{cards_html}</div>
    {_review_script()}"""
    return page("Review", body, active="review")


def _review_script():
    return """
    <style>
      .review-bar { display:flex; align-items:center; justify-content:space-between;
        gap:16px; flex-wrap:wrap;
        background:linear-gradient(160deg,var(--bg-2),var(--bg-1)); border:1px solid var(--line);
        border-radius:var(--radius-lg); padding:14px 18px; margin-bottom:18px; }
      .rb-stats { display:flex; gap:20px; font-size:13px; color:var(--text-2); }
      .rb-stat b { color:var(--text); font-size:16px; font-family:var(--serif); margin-right:4px; }
      .rb-stat.approved b { color:var(--gp-3); }
      .rb-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
      /* every control in the toolbar shares one height for a clean line */
      .rb-actions .sort-wrap select,
      .rb-actions .btn,
      .rb-actions #srcFilterBtn,
      .rb-actions #aiBtn,
      .rb-actions #langSel,
      .rb-actions #relSel,
      .rb-actions #relSel,
      .rb-actions .toggle-decided {
        height:38px; box-sizing:border-box; display:inline-flex; align-items:center;
        font-size:13px; border-radius:9px; white-space:nowrap; }
      .sort-wrap { display:flex; align-items:center; gap:7px; }
      .sort-wrap label { font-size:12px; color:var(--text-3); font-family:var(--mono);
        text-transform:uppercase; letter-spacing:.5px; }
      .sort-wrap select { background:var(--bg-3); color:var(--text); border:1px solid var(--line-2);
        padding:0 30px 0 12px; font-family:var(--sans); cursor:pointer;
        appearance:none; -webkit-appearance:none;
        background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath fill='%238b97a6' d='M0 0l5 6 5-6z'/%3E%3C/svg%3E");
        background-repeat:no-repeat; background-position:right 11px center; }
      .sort-wrap select:hover { border-color:var(--gp); }
      .sort-wrap select:focus { outline:2px solid var(--gp); outline-offset:1px; }
      .rb-actions .btn { padding:0 16px; }
      .toggle-decided { background:var(--bg-3); border:1px solid var(--line-2);
        color:var(--text-2); cursor:pointer; padding:0 14px; }
      .toggle-decided:hover { color:var(--gp-3); border-color:var(--gp); }
      .progress-line { min-height:0; margin-bottom:14px; }
      .progress-row { display:flex; align-items:stretch; gap:12px; margin-bottom:14px; }
      .progress-row .progress-line { flex:1; margin-bottom:0; display:flex; align-items:center; }
      .progress-row .progress-line:empty { display:none; }
      .progress-row .pbar { width:100%; }
      .progress-row .progress-line .pbar { margin-bottom:0; width:100%; }
      .btn-force-stop { display:inline-flex; align-items:center; justify-content:center; gap:6px;
        height:38px; box-sizing:border-box;
        background:linear-gradient(145deg,#ef4444,#dc2626); color:#fff;
        border:none; border-radius:10px; padding:0 18px; font-size:13px;
        font-weight:700; cursor:pointer; white-space:nowrap;
        box-shadow:0 2px 8px rgba(239,68,68,.35);
        transition:transform .1s ease, filter .15s ease; }
      .btn-force-stop:hover { filter:brightness(1.08); }
      .btn-force-stop:active { transform:scale(.94); }
      .btn-force-stop:disabled { opacity:.6; cursor:default; }
      .review-list { display:flex; flex-direction:column; gap:14px; }
      .rcard { display:grid; grid-template-columns:1fr auto; gap:18px;
        background:linear-gradient(160deg,var(--bg-2),var(--bg-1)); border:1px solid var(--line);
        border-radius:var(--radius-lg); padding:20px 22px;
        transition:opacity .4s cubic-bezier(.4,0,.2,1), transform .4s cubic-bezier(.4,0,.2,1), max-height .45s cubic-bezier(.4,0,.2,1), margin .4s ease, padding .4s ease, border-color .25s ease, box-shadow .25s ease;
        overflow:hidden; }
      .rcard.decided-hidden { opacity:0; transform:translateX(40px) scale(.98);
        margin:0; padding-top:0; padding-bottom:0; border-width:0; pointer-events:none; }
      .rcard.flash-approve { border-color:var(--gp); box-shadow:0 0 0 2px rgba(0,181,226,.4), 0 0 30px rgba(0,181,226,.25); }
      .rcard.flash-reject { border-color:var(--reject); box-shadow:0 0 0 2px rgba(226,104,90,.35); }
      .rcard.is-approved { border-color:rgba(0,181,226,.4); }
      .rcard.is-rejected { opacity:.55; }
      .dup-row { transition:opacity .3s ease, max-height .3s ease, margin .3s ease, padding .3s ease, background .2s ease; overflow:hidden; max-height:80px; }
      .dup-row.subrow-keep { background:rgba(0,181,226,.12); }
      .dup-row.subrow-drop { background:rgba(226,104,90,.1); }
      .rcard-meta { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
      .src-chip { font-size:12px; font-weight:600; color:var(--gp-3);
        background:rgba(0,181,226,.1); padding:3px 10px; border-radius:6px; }
      .sec-chip { font-size:11px; color:var(--text-2); font-family:var(--mono); }
      .kw-chip { font-size:11px; color:var(--text-3); font-family:var(--mono); }
      .also-badge { font-size:11px; font-weight:600; color:#04222b;
        background:linear-gradient(145deg,var(--gp-3),var(--gp-2)); padding:3px 10px; border-radius:999px; }
      .status-stamp { font-size:11px; font-weight:700; letter-spacing:.5px; padding:3px 10px;
        border-radius:6px; text-transform:uppercase; display:none; }
      .rcard.is-approved .status-stamp.approved { display:inline-block; color:#042c38;
        background:linear-gradient(145deg,var(--gp-3),var(--gp-2)); }
      .rcard.is-rejected .status-stamp.rejected { display:inline-block; color:var(--reject);
        background:rgba(226,104,90,.15); }
      .rcard-title, .rcard-title:visited { font-family:var(--serif); font-size:19px;
        color:var(--text); text-decoration:none; line-height:1.3; display:block; }
      .rcard-title:hover { color:var(--gp-3); text-decoration:none; }
      .rcard-title-en { font-family:var(--sans); font-size:13px; color:var(--text-3);
        margin-top:4px; font-style:italic; line-height:1.4; }
      .rcard-foot { display:flex; align-items:center; gap:16px; margin-top:10px; }
      .pub { font-size:11px; color:var(--text-3); font-family:var(--mono); letter-spacing:.3px; }
      .expand-btn { background:none; border:0; color:var(--gp-3); font-size:13px;
        cursor:pointer; font-weight:600; padding:0; }
      /* topic grouping: substantial cards + a "+N similar topic" dropdown */
      .topic-more-row { grid-column:1 / -1; padding-left:2px; margin-top:12px; }
      .topic-more { background:none; border:0; color:var(--gp-3); font-size:13px;
        cursor:pointer; font-weight:600; padding:0; font-family:inherit; }
      .topic-more:hover { color:var(--gp-2); }
      .topic-more .topic-label { color:var(--text-3); font-weight:500; }
      .topic-more .topic-arrow { display:inline-block; }
      .topic-hidden { display:flex; flex-direction:column; gap:8px; margin-top:10px; }
      .topic-row { display:grid; grid-template-columns:1fr auto; gap:18px;
        align-items:center; padding:10px 14px; background:var(--bg-2);
        border:1px solid var(--line); border-radius:8px; }
      .topic-row-main { min-width:0; display:flex; flex-direction:column; gap:4px; }
      .topic-row-head { display:flex; align-items:center; gap:8px; }
      .topic-row-title { color:var(--text); font-size:13.5px; text-decoration:none;
        font-weight:500; line-height:1.35; }
      .topic-row-title:hover { color:var(--gp-3); }
      .topic-row-foot { display:flex; align-items:center; gap:14px; margin-top:2px; }
      .topic-row-actions { display:flex; gap:8px; flex:0 0 auto; align-items:center; }
      .act.sm { padding:6px 14px; font-size:12px; }
      /* distinct styling for the "Group topics" pipeline stage button */
      .pipe-ico-topic { background:linear-gradient(145deg,#d97706,#fbbf24);
        color:#3a2600; flex:0 0 30px; height:30px; border-radius:8px;
        display:flex; align-items:center; justify-content:center;
        font-size:14px; font-weight:700; }
      .pipe-stage-topic { background:linear-gradient(145deg,rgba(217,119,6,.14),rgba(251,191,36,.05));
        border-color:rgba(251,191,36,.35); }
      .pipe-stage-topic:hover { border-color:#fbbf24;
        background:linear-gradient(145deg,rgba(217,119,6,.20),rgba(251,191,36,.08)); }
      /* article body toggle */
      .body-btn { display:inline-flex; align-items:center; gap:6px; background:none;
        border:0; color:var(--gp-3); font-size:12.5px; font-weight:600; cursor:pointer;
        padding:3px 0; transition:color .15s ease; font-family:inherit; }
      .body-btn:hover { color:var(--gp-2); }
      .body-arrow { display:inline-block; font-size:15px; line-height:1;
        transition:transform .18s ease; transform:translateY(1px); }
      .body-btn-open .body-arrow { transform:translateY(1px) rotate(0deg); }
      .body-btn-empty { color:var(--text-3); cursor:default; font-weight:500; }
      .body-btn-empty:hover { color:var(--text-3); }
      .body-arrow-none { display:inline-block; opacity:.5; }
      .body-panel { margin-top:12px; animation:bodyFade .22s ease; }
      .body-text { background:rgba(255,255,255,.02); border:1px solid var(--line);
        border-left:3px solid var(--gp-3); border-radius:8px; padding:14px 16px;
        font-size:13.5px; line-height:1.7; color:var(--text-2); max-height:340px;
        overflow-y:auto; white-space:pre-wrap; }
      @keyframes bodyFade { from{opacity:0; transform:translateY(-4px);}
        to{opacity:1; transform:translateY(0);} }
      .rcard-actions { display:flex; flex-direction:column; gap:10px; justify-content:center; }
      .act { font-size:13px; font-weight:600; padding:11px 20px; border-radius:9px;
        border:1px solid transparent; cursor:pointer; min-width:120px;
        display:flex; align-items:center; justify-content:center; gap:7px;
        transition:transform .1s ease, filter .15s ease, background .15s ease; }
      .act:active { transform:scale(.94); }
      .act.approve { color:#042c38; background:linear-gradient(145deg,var(--gp-3),var(--gp-2));
        box-shadow:0 3px 12px rgba(0,181,226,.3); }
      .act.approve:hover { filter:brightness(1.1); box-shadow:0 5px 18px rgba(0,181,226,.45); }
      .act.reject { background:var(--bg-3); color:var(--text-2); border-color:var(--line-2); }
      .act.reject:hover { color:var(--reject); border-color:var(--reject); background:rgba(226,104,90,.08); }
      .act .ico { width:15px; height:15px; display:inline-block; }
      .dup-detail { grid-column:1 / -1; margin-top:14px; border-top:1px solid var(--line);
        padding-top:12px; display:flex; flex-direction:column; gap:8px;
        max-height:340px; overflow-y:auto; }
      .dup-row { display:grid; grid-template-columns:130px 1fr auto; gap:12px; align-items:center;
        padding:8px 10px; background:var(--bg-0); border-radius:8px; }
      .dup-src { font-size:12px; font-weight:600; color:var(--gp-3); }
      .dup-title, .dup-title:visited { font-size:13px; color:var(--text-2);
        text-decoration:none; line-height:1.4; }
      .dup-title:hover { color:var(--gp-3); text-decoration:none; }
      .dup-actions { display:flex; gap:6px; }
      .mini { font-size:11px; font-weight:600; padding:6px 12px; border-radius:6px;
        border:1px solid var(--line-2); background:var(--bg-3); color:var(--text-2); cursor:pointer;
        transition:transform .1s ease; }
      .mini:active { transform:scale(.92); }
      .mini.approve:hover { color:var(--gp-3); border-color:var(--gp); }
      .mini.reject:hover { color:var(--reject); border-color:var(--reject); }
    </style>
    <script>
      let showDecided = false;
      const CHECK = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>';
      const XMARK = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M18 6L6 18M6 6l12 12"/></svg>';

      async function decide(id, status, groupKey) {
        const r = await fetch('/api/review/' + id + '/' + status, {method:'POST'});
        if (!r.ok) return;
        if (groupKey) {
          const card = document.getElementById('card-' + groupKey);
          if (card) {
            // pin current height so the collapse animates smoothly from it
            card.style.maxHeight = card.scrollHeight + 'px';
            card.classList.remove('is-approved','is-rejected','flash-approve','flash-reject');
            card.classList.add(status === 'approved' ? 'flash-approve' : 'flash-reject');
            card.classList.add(status === 'approved' ? 'is-approved' : 'is-rejected');
            card.dataset.status = status;
            setTimeout(() => {
              card.classList.remove('flash-approve','flash-reject');
              if (!showDecided) {
                card.style.maxHeight = '0px';
                card.classList.add('decided-hidden');
              }
            }, 450);
          }
        }
        refreshCounts();
      }
      // grouped sub-article: act on ONE member, fade just that row out
      async function decideSub(id, status, btn) {
        const r = await fetch('/api/review/' + id + '/' + status, {method:'POST'});
        if (!r.ok) return;
        const row = btn.closest('.dup-row');
        if (row) {
          row.classList.add(status === 'approved' ? 'subrow-keep' : 'subrow-drop');
          setTimeout(() => {
            row.style.maxHeight = '0px';
            row.style.opacity = '0';
            row.style.margin = '0';
            row.style.padding = '0';
          }, 30);
        }
        refreshCounts();
      }
      function toggleGroup(key) {
        const d = document.getElementById('detail-' + key);
        d.style.display = d.style.display === 'none' ? 'flex' : 'none';
      }
      function toggleTopic(key) {
        const p = document.getElementById('topic-' + key);
        const b = document.getElementById('topicbtn-' + key);
        if (!p) return;
        const open = p.style.display !== 'none';
        p.style.display = open ? 'none' : 'flex';
        if (b) {
          const arrow = b.querySelector('.topic-arrow');
          if (arrow) arrow.textContent = open ? '›' : '⌄';
        }
      }
      function toggleBody(key) {
        const p = document.getElementById('body-' + key);
        const b = document.getElementById('bodybtn-' + key);
        if (!p) return;
        const open = p.style.display !== 'none';
        p.style.display = open ? 'none' : 'block';
        if (b) {
          b.classList.toggle('body-btn-open', !open);
          const arrow = b.querySelector('.body-arrow');
          if (arrow) arrow.textContent = open ? '›' : '⌄';
          b.childNodes[b.childNodes.length - 1].textContent =
            open ? ' Read article' : ' Hide article';
        }
      }
      function toggleDecided() {
        showDecided = !showDecided;
        const btn = document.getElementById('toggleDecidedBtn');
        btn.textContent = showDecided ? 'Hide decided' : 'Show decided';
        document.querySelectorAll('.rcard').forEach(c => {
          const decided = c.dataset.status === 'approved' || c.dataset.status === 'rejected';
          if (decided) c.classList.toggle('decided-hidden', !showDecided);
        });
      }
      async function refreshCounts() {
        const c = await (await fetch('/api/review/counts')).json();
        const p = document.getElementById('cnt-pending');
        const a = document.getElementById('cnt-approved');
        if (p) p.textContent = c.pending;
        if (a) a.textContent = c.approved;
      }
      // ── unified background-job watcher ──────────────────────────────────
      // One poller drives the progress bar off the server's `active` flag,
      // which is true whenever EITHER collection or the pipeline is running.
      // This is what stops the bar vanishing during the collection→pipeline
      // handoff (the old code polled the two phases separately and died in the
      // gap). When the whole job finishes, we reload once so newly promoted /
      // scored articles appear without the user having to switch tabs.
      let _watching = false;
      let _sawActive = false;
      let _sawCollection = false;

      async function watchJob(opts) {
        opts = opts || {};
        if (_watching) return;          // never run two pollers at once
        _watching = true;
        _sawActive = false;
        _sawCollection = false;
        const prog = document.getElementById('progress');

        const tick = async () => {
          let s;
          try {
            s = await (await fetch('/api/collect/status')).json();
          } catch (e) {
            setTimeout(tick, 1500);     // network hiccup - keep trying
            return;
          }

          if (s.cancelling) {
            if (prog) showBar(prog, 'Stopping…', s.done, s.total, 'warn');
            setTimeout(tick, 800);
            return;
          }

          if (s.active) {
            _sawActive = true;
            // collection phase shows the source being fetched; pipeline phase
            // shows the stage label. Pick whichever is currently running.
            if (s.running) {
              _sawCollection = true;   // remember we saw the fetch phase
              if (prog) showBar(prog, 'Fetching ' + (s.current || ''), s.done, s.total, 'fetch');
            } else {
              // ── collection → pipeline handoff ──
              // Fetch is done; matched articles are already written as
              // 'pending' in the DB (collection writes them pending BEFORE any
              // translation). If we watched the collection phase this run and
              // haven't yet reloaded for the handoff, reload ONCE now so those
              // articles appear immediately - the user reviews them while the
              // pipeline (translate → semantic → dedup → judge) runs on. After
              // the reload, resumeWatch() re-attaches this poller for the
              // pipeline phase, which reloads again at final completion to show
              // scores / groupings / semantic promotions.
              if (_sawCollection && !sessionStorage.getItem('_handoffReloaded')) {
                sessionStorage.setItem('_handoffReloaded', '1');
                _watching = false;
                if (prog) showBar(prog, 'Fetched - loading articles…', 1, 1, 'done');
                setTimeout(() => location.reload(), 400);
                return;
              }
              const llm = s.llm || {};
              if (prog) showBar(prog, llm.step || 'Processing', llm.done, llm.total, 'llm');
            }
            setTimeout(tick, 1200);
            return;
          }

          // ── nothing active anymore ──
          _watching = false;
          sessionStorage.removeItem('_handoffReloaded');   // clear for next run
          if (s.error) {
            if (prog) showBar(prog, 'Error: ' + s.error, 0, 0, 'warn');
            return;
          }
          // If we watched a real run go from active → idle, the DB now has new
          // articles / scores that aren't on screen. Reload once so they show.
          if (_sawActive || opts.reloadOnDone) {
            if (prog) showBar(prog, 'Done - loading results…', 1, 1, 'done');
            setTimeout(() => location.reload(), 600);
          }
        };
        tick();
      }

      async function runCollection() {
        // #runBtn only exists on the empty-state review page; the pipeline
        // dropdown's "Re-run collection" button has no matching id, so guard it.
        const btn = document.getElementById('runBtn');
        const prog = document.getElementById('progress');
        const old = btn ? btn.textContent : null;
        if (btn) { btn.disabled = true; btn.textContent = 'Collecting…'; }
        const start = await fetch('/api/collect/start', {method:'POST'});
        if (!start.ok) {
          showBar(prog, 'A collection is already running.', 0, 0, 'warn');
          if (btn) { btn.disabled = false; btn.textContent = old; }
          // still attach the watcher so the existing run's bar shows
          watchJob();
          return;
        }
        showStopButton(true);
        showBar(prog, 'Starting collection…', 0, 0, 'fetch');
        watchJob({reloadOnDone: true});
      }

      // On page load and whenever the tab regains focus, resume watching any
      // job that's still running (survives reloads and tab switches).
      async function resumeWatch() {
        try {
          const s = await (await fetch('/api/collect/status')).json();
          if (s.active) { showStopButton(true); watchJob(); }
          else showStopButton(false);
        } catch (e) { /* ignore */ }
      }
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) resumeWatch();
      });

      // ── Force Stop ──────────────────────────────────────────────────────
      function showStopButton(on) {
        const b = document.getElementById('forceStopBtn');
        if (b) b.style.display = on ? 'inline-flex' : 'none';
      }
      async function forceStop() {
        const b = document.getElementById('forceStopBtn');
        if (b) { b.disabled = true; b.textContent = '⏹ Stopping…'; }
        try { await fetch('/api/stop', {method:'POST'}); } catch (e) {}
        // watcher will see `cancelling` then idle, and reload
      }
      function togglePipeline() {
        const d = document.getElementById('pipeDropdown');
        d.style.display = d.style.display === 'none' ? 'block' : 'none';
      }
      function closePipeline() {
        const d = document.getElementById('pipeDropdown');
        if (d) d.style.display = 'none';
      }
      const STAGE_LABEL = {
        translate: 'Starting translation',
        semantic: 'Starting semantic filter',
        bodies: 'Fetching article text',
        dedup: 'Starting grouping',
        judge: 'Starting scoring',
        topic: 'Grouping topics',
        '': 'Starting AI analysis'
      };
      async function runStage(stage) {
        closePipeline();
        const prog = document.getElementById('progress');
        const url = '/api/llm/start' + (stage ? ('?stage=' + stage) : '');
        const r = await fetch(url, {method:'POST'});
        if (r.ok) {
          showStopButton(true);
          showBar(prog, STAGE_LABEL[stage] || 'Starting', 0, 0, 'llm');
          watchJob({reloadOnDone: true});
        }
        else if (r.status === 409) { showBar(prog, 'A stage is already running', 0, 0, 'warn'); watchJob(); }
        else { showBar(prog, 'Could not start stage', 0, 0, 'warn'); }
      }
      // close the pipeline dropdown when clicking outside it
      document.addEventListener('click', (e) => {
        const wrap = document.getElementById('pipeDropdown');
        const btn = document.getElementById('pipelineBtn');
        if (wrap && wrap.style.display === 'block' &&
            !wrap.contains(e.target) && e.target !== btn) {
          wrap.style.display = 'none';
        }
      });
      // a clean progress bar (replaces the old plain text line)
      function showBar(el, label, done, total, kind) {
        const pct = total > 0 ? Math.round(done/total*100) : (kind==='warn'?0:8);
        const cls = kind === 'warn' ? 'pb-warn' : (kind==='done' ? 'pb-done' : '');
        el.innerHTML =
          '<div class="pbar ' + cls + '">' +
            '<div class="pbar-fill" style="width:' + pct + '%"></div>' +
            '<div class="pbar-label">' + label + (total>0 ? ' · ' + done + '/' + total : '') + '</div>' +
          '</div>';
      }
      window.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.act.approve').forEach(b => { if(!b.querySelector('svg')) b.insertAdjacentHTML('afterbegin', CHECK); });
        document.querySelectorAll('.act.reject').forEach(b => { if(!b.querySelector('svg')) b.insertAdjacentHTML('afterbegin', XMARK); });
        resumeWatch();   // pick up any background job still running after a reload
      });
      async function clearArticles() {
        const scope = confirm("Clear ALL fetched articles and review decisions?\\n\\nOK = clear everything\\nCancel = keep everything")
          ? 'all' : null;
        if (!scope) return;
        const sure = confirm("This permanently deletes all collected articles and your approve/reject decisions. Continue?");
        if (!sure) return;
        const r = await fetch('/api/articles/clear?scope=' + scope, {method:'POST'});
        if (r.ok) location.reload();
      }
      function changeSort(value) {
        const u = new URL(window.location);
        u.searchParams.set('sort', value);
        window.location = u.toString();
      }
      function changeColors() {
        const picked = [];
        ['red','yellow','green','magenta'].forEach(c => {
          if (document.getElementById('cf_' + c).checked) picked.push(c);
        });
        const u = new URL(window.location);
        // all 4 (or none) = no filter
        if (picked.length === 0 || picked.length === 4) u.searchParams.delete('colors');
        else u.searchParams.set('colors', picked.join(','));
        window.location = u.toString();
      }
      function toggleRelFilter() {
        const d = document.getElementById('relDropdown');
        d.style.display = d.style.display === 'none' ? 'block' : 'none';
      }
      function setAllRel(on) {
        document.querySelectorAll('#relDropdown input[type=checkbox]').forEach(c => c.checked = on);
      }
      function applyRelFilter() {
        const picked = [];
        ['red','yellow','green','magenta'].forEach(c => {
          if (document.getElementById('cf_' + c).checked) picked.push(c);
        });
        const u = new URL(window.location);
        if (picked.length === 0 || picked.length === 4) u.searchParams.delete('colors');
        else u.searchParams.set('colors', picked.join(','));
        window.location = u.toString();
      }
      function changeLang(value) {
        const u = new URL(window.location);
        if (!value) u.searchParams.delete('lang');
        else u.searchParams.set('lang', value);
        window.location = u.toString();
      }
      function changeMatch(value) {
        const u = new URL(window.location);
        if (!value) u.searchParams.delete('match');
        else u.searchParams.set('match', value);
        window.location = u.toString();
      }
      function toggleSrcFilter() {
        const d = document.getElementById('srcDropdown');
        d.style.display = d.style.display === 'none' ? 'block' : 'none';
      }
      function setAllSrc(on) {
        document.querySelectorAll('#srcDropdown input[type=checkbox]').forEach(c => c.checked = on);
      }
      function applySrcFilter() {
        const checked = [...document.querySelectorAll('#srcDropdown input:checked')].map(c => c.value);
        const all = document.querySelectorAll('#srcDropdown input').length;
        const u = new URL(window.location);
        if (checked.length === 0 || checked.length === all) u.searchParams.delete('sources');
        else u.searchParams.set('sources', checked.join(','));
        window.location = u.toString();
      }
    </script>"""


@app.get("/sources", response_class=HTMLResponse)
def sources_page():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, method, config, language, enabled FROM sources ORDER BY name"
    ).fetchall()
    conn.close()

    def _display_methods(method, config_json):
        """Show every method this source will actually use during exhaustive
        collection, spelled out clearly. Mirrors fetch_all_methods() logic."""
        import json as _json
        try:
            cfg = _json.loads(config_json or "{}")
        except Exception:
            cfg = {}
        disabled = set(cfg.get("disable_methods", []))

        # primary method display name
        primary_map = {
            "api": "API", "sitemap": "sitemap", "sitemap_index_merge": "sitemap",
            "bbc_sitemaps": "sitemap", "aljazeera_dated": "sitemap",
            "dated_sitemap": "sitemap", "page_dates": "sitemap",
            "rss": "RSS", "rss_multi": "RSS", "gnews": "Google News",
            "id_enum": "ID scan", "daily_star_nodes": "ID scan",
            "wp_rest": "API", "combine": "sitemap",
            "ajax_archive": "archive", "amarbarta_archive": "archive",
            "curl_sitemap": "sitemap",
        }
        methods = []
        primary = primary_map.get(method, method)
        methods.append(primary)

        # exhaustive collection adds sitemap + RSS + Google News for real domains
        # (unless the method already IS that, or it's disabled)
        has_domain = bool(cfg.get("url") or cfg.get("site_url"))
        if has_domain:
            if "sitemap" not in disabled and "sitemap" not in methods:
                methods.append("sitemap")
            if "rss" not in disabled and "RSS" not in methods:
                methods.append("RSS")
            if "gnews" not in disabled and method != "gnews" and "Google News" not in methods:
                methods.append("Google News")

        # order and dedup
        order = ["API", "sitemap", "RSS", "Google News", "ID scan"]
        methods = list(dict.fromkeys(methods))
        methods.sort(key=lambda m: order.index(m) if m in order else 99)
        return methods

    trs = ""
    for r in rows:
        status = ('<span class="pill live"><span class="dot"></span>on</span>'
                  if r["enabled"]
                  else '<span class="pill off"><span class="dot"></span>off</span>')
        toggle_label = "Disable" if r["enabled"] else "Enable"
        method_tags = "".join(
            f'<span class="method-tag">{m}</span>'
            for m in _display_methods(r["method"], r["config"]))
        trs += f"""
        <tr id="src-{r['id']}">
          <td class="name">{r['name']}</td>
          <td>{method_tags}</td>
          <td><span class="lang-tag">{r['language']}</span></td>
          <td>{status}</td>
          <td class="row-actions">
            <button class="mini" onclick="editSource({r['id']})">edit</button>
            <button class="mini" onclick="toggleSource({r['id']})">{toggle_label.lower()}</button>
            <button class="mini reject" onclick="deleteSource({r['id']},'{r['name']}')">delete</button>
          </td>
        </tr>"""

    body = f"""
    <div class="page-head"><div class="eyebrow">Configuration</div>
    <h1>Sources</h1><p>The {len(rows)} news sources feeding the desk. Add, edit,
    enable/disable, or test any source - no code changes needed.</p>
    <hr class="signal-rule"></div>

    <div class="panel">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <h2 style="margin:0;">All sources</h2>
        <button class="btn btn-primary" onclick="openAddSource()">+ Add source</button>
      </div>
      <div id="testResult" class="test-result" style="display:none;"></div>
      <table class="dlist">
        <tr><th>Source</th><th>Method</th><th>Lang</th><th>Status</th><th>Actions</th></tr>
        {trs}
      </table>
    </div>

    <!-- add/edit modal -->
    <div id="srcModal" class="modal-bg" style="display:none;">
      <div class="modal">
        <h2 id="modalTitle">Add source</h2>
        <input type="hidden" id="f_id">
        <label>Name</label>
        <input id="f_name" placeholder="e.g. New Outlet">
        <label>Method</label>
        <select id="f_method">
          <option value="sitemap">sitemap</option>
          <option value="rss">rss</option>
          <option value="rss_multi">rss_multi (several feeds)</option>
          <option value="gnews">gnews (Google News)</option>
          <option value="api">api (JSON API)</option>
          <option value="page_dates">page_dates (fetch dates from pages)</option>
          <option value="combine">combine (sitemap + feeds)</option>
        </select>
        <label>URL <span class="lbl-hint">(gnews: domain only · api: full endpoint URL)</span></label>
        <input id="f_url" placeholder="https://example.com/news-sitemap.xml">
        <label>Extra config (JSON, optional) <span class="lbl-hint">api: items_path, title_field, url_field, date_field · others: fallbacks</span></label>
        <textarea id="f_config" rows="3" placeholder='{{"items_path": "articles", "title_field": "title", "url_field": "url", "date_field": "publishedAt"}}'></textarea>
        <label>Extra collection methods <span class="lbl-hint">collection runs all enabled methods and merges results</span></label>
        <div class="method-toggles">
          <label class="mt"><input type="checkbox" id="m_sitemap" checked> sitemap</label>
          <label class="mt"><input type="checkbox" id="m_rss" checked> rss</label>
          <label class="mt"><input type="checkbox" id="m_gnews" checked> gnews</label>
        </div>
        <label>Language</label>
        <select id="f_lang"><option value="en">en</option><option value="bn">bn</option><option value="mixed">mixed</option></select>
        <div class="modal-actions">
          <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary" onclick="saveSource()">Save</button>
        </div>
      </div>
    </div>
    {_sources_script()}"""
    return page("Sources", body, active="sources")


def _sources_script():
    return """
    <style>
      .row-actions { display:flex; gap:6px; }
      .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,.7); backdrop-filter:blur(4px);
        display:flex; align-items:center; justify-content:center; z-index:100; }
      .modal { background:var(--bg-2); border:1px solid var(--line-2); border-radius:var(--radius-lg);
        padding:28px 30px; width:520px; max-width:92vw; max-height:88vh; overflow-y:auto; }
      .modal h2 { font-family:var(--sans); margin:0 0 18px; font-size:22px; }
      .modal label { display:block; font-size:13px; color:var(--text-2); margin:14px 0 6px; font-weight:500; }
      .lbl-hint { color:var(--text-3); font-weight:400; font-size:11px; }
      .modal input, .modal select, .modal textarea { width:100%; background:var(--bg-0);
        border:1px solid var(--line-2); border-radius:8px; padding:10px 12px; color:var(--text);
        font-size:14px; font-family:var(--sans); }
      .modal textarea { font-family:var(--mono); font-size:12px; }
      .modal-actions { display:flex; gap:10px; justify-content:flex-end; margin-top:22px; }
      .method-toggles { display:flex; gap:16px; padding:4px 0; }
      .method-toggles .mt { display:flex; align-items:center; gap:6px; margin:0;
        font-size:13px; color:var(--text-2); cursor:pointer; width:auto; }
      .method-toggles .mt input { width:auto; accent-color:var(--gp); }
    </style>
    <script>
      function openAddSource() {
        document.getElementById('modalTitle').textContent = 'Add source';
        document.getElementById('f_id').value = '';
        ['f_name','f_url','f_config'].forEach(i => document.getElementById(i).value='');
        document.getElementById('f_method').value = 'sitemap';
        document.getElementById('f_lang').value = 'en';
        ['m_sitemap','m_rss','m_gnews'].forEach(i => document.getElementById(i).checked = true);
        document.getElementById('srcModal').style.display = 'flex';
      }
      async function editSource(id) {
        const s = await (await fetch('/api/sources/' + id)).json();
        document.getElementById('modalTitle').textContent = 'Edit source';
        document.getElementById('f_id').value = s.id;
        document.getElementById('f_name').value = s.name;
        document.getElementById('f_method').value = s.method;
        const cfg = s.config || {};
        document.getElementById('f_url').value = cfg.url || (cfg.urls ? '' : '');
        // show remaining config minus url
        const rest = Object.assign({}, cfg); delete rest.url; delete rest.disable_methods;
        document.getElementById('f_config').value = Object.keys(rest).length ? JSON.stringify(rest) : '';
        const dis = (cfg.disable_methods || []);
        document.getElementById('m_sitemap').checked = !dis.includes('sitemap');
        document.getElementById('m_rss').checked = !dis.includes('rss');
        document.getElementById('m_gnews').checked = !dis.includes('gnews');
        document.getElementById('f_lang').value = s.language || 'en';
        document.getElementById('srcModal').style.display = 'flex';
      }
      function closeModal() { document.getElementById('srcModal').style.display = 'none'; }
      async function saveSource() {
        let config = {};
        const extra = document.getElementById('f_config').value.trim();
        if (extra) { try { config = JSON.parse(extra); } catch(e) { alert('Extra config is not valid JSON'); return; } }
        const url = document.getElementById('f_url').value.trim();
        if (url) config.url = url;
        const disable = [];
        if (!document.getElementById('m_sitemap').checked) disable.push('sitemap');
        if (!document.getElementById('m_rss').checked) disable.push('rss');
        if (!document.getElementById('m_gnews').checked) disable.push('gnews');
        if (disable.length) config.disable_methods = disable;
        const payload = {
          id: document.getElementById('f_id').value || null,
          name: document.getElementById('f_name').value.trim(),
          method: document.getElementById('f_method').value,
          config: config,
          language: document.getElementById('f_lang').value
        };
        if (!payload.name) { alert('Name is required'); return; }
        const r = await fetch('/api/sources/save', {method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
        if (r.ok) location.reload(); else alert('Save failed');
      }
      async function toggleSource(id) {
        const r = await fetch('/api/sources/' + id + '/toggle', {method:'POST'});
        if (r.ok) location.reload();
      }
      async function deleteSource(id, name) {
        if (!confirm('Delete source "' + name + '"? This cannot be undone.')) return;
        const r = await fetch('/api/sources/' + id, {method:'DELETE'});
        if (r.ok) location.reload();
      }
      async function testSource(id, name) {
        const out = document.getElementById('testResult');
        out.style.display = 'block';
        out.className = 'test-result testing';
        out.textContent = 'Testing ' + name + '… fetching live, this can take a few seconds';
        try {
          const r = await fetch('/api/sources/' + id + '/test', {method:'POST'});
          const d = await r.json();
          if (d.ok) {
            out.className = 'test-result ok';
            out.textContent = '✓ ' + name + ' - ' + d.total + ' fetched, ' + d.count + ' from today (via ' + d.method + ')';
          } else {
            out.className = 'test-result fail';
            out.textContent = '✗ ' + name + ' - ' + (d.error || 'no articles found');
          }
        } catch(e) {
          out.className = 'test-result fail';
          out.textContent = '✗ ' + name + ' - request failed';
        }
      }
      async function diagnoseSource(id, name) {
        const out = document.getElementById('testResult');
        out.style.display = 'block';
        out.className = 'test-result testing';
        out.textContent = 'Diagnosing ' + name + '… running step-by-step checks, this can take up to a minute';
        try {
          const r = await fetch('/api/sources/' + id + '/diagnose', {method:'POST'});
          const d = await r.json();
          out.className = 'test-result ' + (d.ok ? 'ok' : 'fail');
          let html = '<b>' + (d.ok ? '✓' : '✗') + ' Diagnosis: ' + name +
                     '</b> <span style="opacity:.7">(' + (d.method || '') + ')</span><br>';
          for (const c of (d.checks || [])) {
            html += '<div style="margin-top:6px;">' +
                    (c.ok ? '<span style="color:var(--ok,#22c55e)">✓</span> ' :
                            '<span style="color:var(--bad,#ef4444)">✗</span> ') +
                    '<b>' + c.step + '</b> - ' +
                    '<span style="opacity:.85">' + c.detail.replace(/</g,'&lt;') + '</span></div>';
          }
          out.innerHTML = html;
        } catch(e) {
          out.className = 'test-result fail';
          out.textContent = '✗ ' + name + ' - diagnose request failed';
        }
      }
    </script>"""


@app.get("/debug", response_class=HTMLResponse)
def debug_page():
    """Visual dashboard: source health from the latest run, plus run history.
    Turns the verify/inspect tools into a glanceable page."""
    import json as _j
    from datetime import date
    conn = get_connection()
    today = date.today().isoformat()

    # latest run + its per-source details
    last_run = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    recent_runs = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 10").fetchall()

    # today's article counts per source (collected + matched)
    src_today = conn.execute(
        """SELECT source, COUNT(*) c,
                  SUM(CASE WHEN review_status!='filtered_out' THEN 1 ELSE 0 END) matched
           FROM articles WHERE day=? GROUP BY source""", (today,)).fetchall()
    today_map = {r["source"]: (r["c"], r["matched"] or 0) for r in src_today}

    all_sources = conn.execute(
        "SELECT id, name, method, enabled FROM sources ORDER BY name").fetchall()
    conn.close()

    # build per-source health rows
    details = _j.loads(last_run["details"]) if last_run and last_run["details"] else {}
    health_rows = ""
    healthy = warning = 0
    for s in all_sources:
        name = s["name"]
        d = details.get(name, {})
        collected, matched = today_map.get(name, (0, 0))
        status = d.get("status", "-")
        method = d.get("method") or s["method"]
        if not s["enabled"]:
            badge = '<span class="pill off"><span class="dot"></span>disabled</span>'
        elif status.startswith("error"):
            badge = '<span class="pill off" style="color:var(--reject)"><span class="dot" style="background:var(--reject)"></span>error</span>'
            warning += 1
        elif collected > 0:
            badge = '<span class="pill live"><span class="dot"></span>healthy</span>'
            healthy += 1
        elif status == "ok" and d.get("fetched", 0) == 0:
            # the method ran cleanly and the site returned NOTHING - it simply
            # published nothing today. Not a fault.
            badge = ('<span class="pill off" style="opacity:.75">'
                     '<span class="dot"></span>no articles today</span>')
            healthy += 1
        elif status == "ok" and (d.get("dropped_old", 0) + d.get("dupe", 0)) >= d.get("fetched", 0) > 0:
            # everything the fetch returned is accounted for: dated older than
            # today (stale site) or already stored (nothing new). Working fine.
            lbl = "nothing new" if d.get("dupe", 0) >= d.get("dropped_old", 0) else "no articles today"
            badge = ('<span class="pill off" style="opacity:.75">'
                     f'<span class="dot"></span>{lbl}</span>')
            healthy += 1
        else:
            # fetched articles but stored none → dates rejected / parse broken.
            # This IS a fault and must show up in Need attention.
            badge = '<span class="pill off" style="color:var(--reject)"><span class="dot" style="background:var(--reject)"></span>no articles</span>'
            warning += 1
        err = ""
        if status.startswith("error"):
            err = f'<span class="src-note src-note-bad">{status}</span>'
        elif collected == 0 and d.get("fetched", 0) > 0:
            # explain WHY a working fetch stored nothing
            f_n = d.get("fetched", 0)
            old_n = d.get("dropped_old", 0)
            dup_n = d.get("dupe", 0)
            bits = [f"fetched {f_n}"]
            if old_n:
                bits.append(f"{old_n} older than today")
            if dup_n:
                bits.append(f"{dup_n} already stored")
            unacc = f_n - old_n - dup_n
            if unacc > 0:
                bits.append(f"{unacc} dropped - no/invalid date")
            # muted note when the source is fine; red only when it's a fault
            cls = "src-note-bad" if unacc > 0 else "src-note"
            err = f'<span class="src-note {cls}">{" · ".join(bits)}</span>'
        health_rows += f"""
        <tr>
          <td class="name">{name}</td>
          <td><span class="method-tag">{method}</span></td>
          <td>{collected}</td>
          <td>{matched}</td>
          <td>{badge} {err}</td>
          <td class="row-actions">
            <button class="mini" onclick="testSource({s['id']},'{name}')">test</button>
            <button class="mini" onclick="diagnoseSource({s['id']},'{name}')">diagnose</button>
          </td>
        </tr>"""

    # run history rows
    hist_rows = ""
    for r in recent_runs:
        when = (r["started_at"] or "")[:16]
        dur = ""
        if r["finished_at"]:
            dur = "done"
        hist_rows += f"""
        <tr>
          <td>{when}</td>
          <td><span class="method-tag">{r['trigger']}</span></td>
          <td>{r['total_new']}</td>
          <td>{r['total_matched']}</td>
          <td>{r['status']}</td>
        </tr>"""
    if not hist_rows:
        hist_rows = '<tr><td colspan="5" style="color:var(--text-2);padding:18px;">No runs yet. Run a collection from the Review page.</td></tr>'

    last_when = (last_run["started_at"][:16] if last_run else "never")

    body = f"""
    <div class="page-head"><div class="eyebrow">Diagnostics</div>
    <h1>Debug &amp; status</h1><p>Source health from the latest collection and a
    history of recent runs. Last run: {last_when}.</p>
    <hr class="signal-rule"></div>

    <div class="stat-grid">
      <div class="stat accent"><div class="idx">01</div><div class="num">{healthy}</div><div class="lbl">Healthy sources</div></div>
      <div class="stat"><div class="idx">02</div><div class="num">{warning}</div><div class="lbl">Need attention</div></div>
      <div class="stat"><div class="idx">03</div><div class="num">{last_run['total_new'] if last_run else 0}</div><div class="lbl">Collected last run</div></div>
      <div class="stat"><div class="idx">04</div><div class="num">{last_run['total_matched'] if last_run else 0}</div><div class="lbl">Matched last run</div></div>
    </div>

    <div class="panel">
      <h2>Source health</h2>
      <p class="hint">Articles collected today per source. "No articles" may mean
         a quiet day or a source that needs checking - use Test on the Sources page.</p>
      <table class="dlist">
        <tr><th>Source</th><th>Method</th><th>Collected</th><th>Matched</th><th>Status</th><th></th></tr>
        {health_rows}
      </table>
    </div>

    <div class="panel">
      <h2>Recent runs</h2>
      <table class="dlist">
        <tr><th>Started</th><th>Trigger</th><th>New</th><th>Matched</th><th>Status</th></tr>
        {hist_rows}
      </table>
    </div>

    <div id="testResult" class="test-result" style="display:none;"></div>
    <script>
      async function testSource(id, name) {{
        const out = document.getElementById('testResult');
        out.style.display = 'block';
        out.className = 'test-result testing';
        out.textContent = 'Testing ' + name + '… fetching live, this can take a few seconds';
        out.scrollIntoView({{behavior:'smooth', block:'nearest'}});
        try {{
          const r = await fetch('/api/sources/' + id + '/test', {{method:'POST'}});
          const d = await r.json();
          if (d.ok) {{
            out.className = 'test-result ok';
            out.textContent = '✓ ' + name + ' - ' + d.total + ' fetched, ' + d.count + ' from today (via ' + d.method + ')';
          }} else {{
            out.className = 'test-result fail';
            out.textContent = '✗ ' + name + ' - ' + (d.error || 'no articles found');
          }}
        }} catch(e) {{
          out.className = 'test-result fail';
          out.textContent = '✗ ' + name + ' - request failed';
        }}
      }}
      async function diagnoseSource(id, name) {{
        const out = document.getElementById('testResult');
        out.style.display = 'block';
        out.className = 'test-result testing';
        out.textContent = 'Diagnosing ' + name + '… running step-by-step checks, this can take up to a minute';
        out.scrollIntoView({{behavior:'smooth', block:'nearest'}});
        try {{
          const r = await fetch('/api/sources/' + id + '/diagnose', {{method:'POST'}});
          const d = await r.json();
          out.className = 'test-result ' + (d.ok ? 'ok' : 'fail');
          let html = '<b>' + (d.ok ? '✓' : '✗') + ' Diagnosis: ' + name +
                     '</b> <span style="opacity:.7">(' + (d.method || '') + ')</span><br>';
          for (const c of (d.checks || [])) {{
            html += '<div style="margin-top:6px;">' +
                    (c.ok ? '<span style="color:#22c55e">✓</span> ' :
                            '<span style="color:#ef4444">✗</span> ') +
                    '<b>' + c.step + '</b> - ' +
                    '<span style="opacity:.85">' + c.detail.replace(/</g,'&lt;') + '</span></div>';
          }}
          out.innerHTML = html;
        }} catch(e) {{
          out.className = 'test-result fail';
          out.textContent = '✗ ' + name + ' - diagnose request failed';
        }}
      }}
    </script>"""
    return page("Debug", body, active="debug")


# ── JSON API endpoints ────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    conn = get_connection()
    data = {
        "sources":  conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
        "keywords": conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0],
        "articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
        "users":    conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
    }
    conn.close()
    return data


@app.get("/api/sources")
def list_sources():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, method, language, enabled FROM sources ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/llm")
def llm_status():
    """Shows the modular LLM layer's current state (off until you add a model)."""
    return provider_status()


# ── Phase 2: collection + review + export endpoints ───────────────────────────
from fastapi import HTTPException
from fastapi.responses import FileResponse
import jobs


@app.post("/api/collect/start")
def collect_start():
    """Kick off a background collection run."""
    started = jobs.start_collection(trigger="manual")
    if not started:
        raise HTTPException(status_code=409, detail="A collection is already running")
    return {"started": True}


@app.get("/api/collect/status")
def collect_status():
    """Poll the running collection's progress."""
    return jobs.get_status()


@app.post("/api/collect/status")
def collect_status_post():
    """POST alias so the status poll works from either verb."""
    return jobs.get_status()


@app.post("/api/stop")
def stop_all():
    """Force Stop: signal any running collection or pipeline to halt at its next
    safe checkpoint. Partial results already written to the DB are kept."""
    return jobs.request_stop()


@app.post("/api/llm/start")
def llm_start(stage: str = ""):
    """(Re-)run the AI pipeline on today's pending articles.
    Optional `stage` query param runs just one stage:
      translate | dedup | judge
    No stage = full run (translate → dedup → judge)."""
    valid = {"translate", "dedup", "judge", "bodies", "semantic", "topic"}
    stages = None
    if stage:
        if stage not in valid:
            raise HTTPException(status_code=400, detail=f"invalid stage: {stage}")
        stages = {stage}
    started = jobs.start_llm(stages=stages)
    if not started:
        raise HTTPException(status_code=409, detail="LLM enrichment already running")
    return {"started": True, "stage": stage or "all"}


@app.post("/api/review/{article_id}/{decision}")
def review_decide(article_id: int, decision: str):
    """Approve or reject one article (by its id)."""
    if decision not in ("approved", "rejected", "pending"):
        raise HTTPException(status_code=400, detail="invalid decision")
    conn = get_connection()
    conn.execute(
        "UPDATE articles SET review_status=?, reviewed_by=?, reviewed_at=datetime('now') "
        "WHERE id=?",
        (decision, "admin", article_id)
    )
    conn.commit()
    conn.close()
    return {"id": article_id, "status": decision}


@app.get("/api/review/counts")
def review_counts():
    """Current pending/approved counts for today (for live UI updates)."""
    from datetime import date
    conn = get_connection()
    today = date.today().isoformat()
    pending = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE day=? AND review_status='pending'",
        (today,)).fetchone()[0]
    approved = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE day=? AND review_status='approved'",
        (today,)).fetchone()[0]
    conn.close()
    return {"pending": pending, "approved": approved}


@app.get("/export")
def export_approved():
    """Export today's approved articles to an Excel file and download it.
    One row per approved story, with an 'also reported by' note."""
    from datetime import date
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    conn = get_connection()
    today = date.today().isoformat()
    rows = conn.execute(
        """SELECT source, title, url, published, sections, keywords, also_in
           FROM articles
           WHERE day=? AND review_status='approved'
           ORDER BY sections, source""",
        (today,)
    ).fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Approved"
    headers = ["Section", "Source", "Headline", "Keywords", "Published",
               "Also reported by", "Link"]
    header_fill = PatternFill("solid", start_color="00B5E2")
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.fill = header_fill
        c.font = Font(bold=True, color="04222B")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for rn, r in enumerate(rows, start=2):
        ws.cell(row=rn, column=1, value=r["sections"])
        ws.cell(row=rn, column=2, value=r["source"])
        ws.cell(row=rn, column=3, value=r["title"])
        ws.cell(row=rn, column=4, value=r["keywords"])
        ws.cell(row=rn, column=5, value=r["published"])
        ws.cell(row=rn, column=6, value=r["also_in"])
        link = ws.cell(row=rn, column=7, value=r["url"])
        link.hyperlink = r["url"]
        link.font = Font(color="0563C1", underline="single")
    for i, w in enumerate([18, 18, 60, 26, 16, 28, 45], 1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = "A2"

    out_path = BASE.parent / "data" / f"approved_{today}.xlsx"
    wb.save(out_path)
    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"risk_intel_approved_{today}.xlsx"
    )


def _approved_articles_today(conn, today):
    rows = conn.execute(
        """SELECT source, title, url, sections, summary, llm_score, content_text
           FROM articles
           WHERE day=? AND review_status='approved'
           ORDER BY COALESCE(llm_score,0) DESC""",
        (today,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/search", response_class=HTMLResponse)
def search_page():
    """Full-DB news finder: all-words AND search over title / title_en / body,
    any date, any status. Flat review-styled rows with on-demand body dropdowns,
    newest first, 200 per batch with an appending 'See more'. Read-only - no
    review actions, grouping, or scores."""
    body = """
    <h1>Search</h1>
    <p class="sub">Find any collected article by words - matches title, English
    title, and article body across all dates. Newest first.</p>

    <div class="search-bar">
      <input id="q" type="text" placeholder="Search all news…  (e.g. hormuz oil)"
             autocomplete="off" />
      <button class="btn btn-primary" id="searchBtn" onclick="doSearch()">Search</button>
    </div>
    <div id="meta" class="search-meta"></div>
    <div id="results"></div>
    <div id="moreWrap" style="display:none; text-align:center; margin:18px 0;">
      <button class="btn" id="moreBtn" onclick="loadMore()">See more</button>
    </div>

    <style>
      .sub { color:var(--text-3); font-size:13px; margin:4px 0 0; }
      .search-bar { display:flex; gap:10px; margin:16px 0 6px; }
      .search-bar input { flex:1; background:var(--bg-2); border:1px solid var(--line);
        border-radius:8px; color:var(--text); padding:11px 14px; font-size:14px;
        font-family:inherit; outline:none; }
      .search-bar input:focus { border-color:var(--gp); }
      .search-meta { color:var(--text-3); font-size:12.5px; margin:6px 2px 14px; min-height:16px; }
      .sresult { background:var(--bg-1); border:1px solid var(--line); border-radius:10px;
        padding:14px 16px; margin-bottom:10px; }
      .sresult-meta { display:flex; align-items:center; gap:10px; margin-bottom:6px; flex-wrap:wrap; }
      .sresult-title { color:var(--text); font-size:15px; font-weight:600; text-decoration:none;
        line-height:1.4; display:block; }
      .sresult-title:hover { color:var(--gp-3); }
      .sresult-title-en { color:var(--text-2); font-size:13px; font-style:italic; margin-top:3px; }
      .sresult-foot { display:flex; align-items:center; gap:14px; margin-top:8px; }
      .sbody { margin-top:10px; padding:12px 14px; background:var(--bg-2);
        border:1px solid var(--line); border-radius:8px; color:var(--text-2);
        font-size:13.5px; line-height:1.6; white-space:pre-wrap; }
      .search-empty { color:var(--text-3); font-size:13px; padding:30px 0; text-align:center; }
      /* reused from the review page so this page renders standalone */
      .src-chip { font-size:12px; font-weight:600; color:var(--gp-3);
        background:rgba(0,181,226,.1); padding:3px 10px; border-radius:6px; }
      .pub { font-size:11px; color:var(--text-3); font-family:var(--mono); letter-spacing:.3px; }
      .body-btn { display:inline-flex; align-items:center; gap:6px; background:none;
        border:0; color:var(--gp-3); font-size:12.5px; font-weight:600; cursor:pointer;
        padding:3px 0; transition:color .15s ease; font-family:inherit; }
      .body-btn:hover { color:var(--gp-2); }
      .body-arrow { display:inline-block; font-size:15px; line-height:1;
        transition:transform .18s ease; transform:translateY(1px); }
      .body-btn-empty { color:var(--text-3); cursor:default; font-weight:500; }
      .body-arrow-none { display:inline-block; }
    </style>

    <script>
      let _q = "", _offset = 0, _total = 0;
      const BATCH = 200;

      function esc(s){ const d=document.createElement('div'); d.textContent=s==null?'':s; return d.innerHTML; }
      function hasBengali(t){ return /[\\u0980-\\u09FF]/.test(t||''); }

      document.getElementById('q').addEventListener('keydown', e => {
        if (e.key === 'Enter') doSearch();
      });

      async function doSearch(){
        _q = document.getElementById('q').value.trim();
        _offset = 0; _total = 0;
        document.getElementById('results').innerHTML = "";
        document.getElementById('moreWrap').style.display = "none";
        if (!_q){ document.getElementById('meta').textContent = ""; return; }
        document.getElementById('meta').textContent = "Searching…";
        await loadMore();
      }

      async function loadMore(){
        const btn = document.getElementById('moreBtn');
        btn.disabled = true; btn.textContent = "Loading…";
        try {
          const r = await fetch('/api/search?q=' + encodeURIComponent(_q)
                    + '&offset=' + _offset + '&limit=' + BATCH);
          const data = await r.json();
          _total = data.total;
          renderRows(data.results);
          _offset += data.results.length;
          document.getElementById('meta').textContent =
            _total === 0 ? "No matches."
            : "Showing " + _offset + " of " + _total.toLocaleString();
          document.getElementById('moreWrap').style.display =
            (_offset < _total) ? "block" : "none";
        } catch(e){
          document.getElementById('meta').textContent = "Search failed.";
        } finally {
          btn.disabled = false; btn.textContent = "See more";
        }
      }

      function renderRows(rows){
        const box = document.getElementById('results');
        if (_offset === 0 && rows.length === 0){
          box.innerHTML = '<div class="search-empty">No articles match those words.</div>';
          return;
        }
        for (const a of rows){
          const div = document.createElement('div');
          div.className = 'sresult';
          const titleEn = (a.title_en && hasBengali(a.title) && a.title_en !== a.title)
            ? '<div class="sresult-title-en">' + esc(a.title_en) + '</div>' : '';
          const bodyCtrl = a.has_body
            ? '<button class="body-btn" id="sbtn-'+a.id+'" onclick="toggleSBody('+a.id+')">'
              + '<span class="body-arrow">›</span> Read article</button>'
            : '<span class="body-btn body-btn-empty"><span class="body-arrow-none">-</span> No article text</span>';
          div.innerHTML =
            '<div class="sresult-meta">'
              + '<span class="src-chip">' + esc(a.source) + '</span>'
              + '<span class="pub">' + esc(a.day) + '</span>'
            + '</div>'
            + '<a class="sresult-title" href="' + esc(a.url) + '" target="_blank">' + esc(a.title) + '</a>'
            + titleEn
            + '<div class="sresult-foot">' + bodyCtrl + '</div>'
            + '<div class="sbody" id="sbody-'+a.id+'" style="display:none;"></div>';
          box.appendChild(div);
        }
      }

      async function toggleSBody(id){
        const pnl = document.getElementById('sbody-'+id);
        const btn = document.getElementById('sbtn-'+id);
        const arrow = btn.querySelector('.body-arrow');
        const open = pnl.style.display !== 'none';
        if (open){
          pnl.style.display = 'none';
          if (arrow) arrow.textContent = '›';
          btn.childNodes[btn.childNodes.length-1].textContent = ' Read article';
          return;
        }
        if (!pnl.dataset.loaded){
          pnl.textContent = 'Loading…';
          pnl.style.display = 'block';
          try {
            const r = await fetch('/api/search/body?id=' + id);
            const d = await r.json();
            pnl.textContent = d.body || 'No article text.';
            pnl.dataset.loaded = "1";
          } catch(e){ pnl.textContent = 'Failed to load.'; }
        } else {
          pnl.style.display = 'block';
        }
        if (arrow) arrow.textContent = '⌄';
        btn.childNodes[btn.childNodes.length-1].textContent = ' Hide article';
      }
    </script>
    """
    return page("Search", body, active="search")


@app.get("/api/search")
def api_search(q: str = "", offset: int = 0, limit: int = 200):
    """All-words AND search over title / title_en / content_text, newest first.
    Returns a paginated batch plus the total match count."""
    q = (q or "").strip()
    if not q:
        return {"results": [], "total": 0}
    words = [w for w in q.split() if w]
    if not words:
        return {"results": [], "total": 0}
    limit = max(1, min(500, limit))
    offset = max(0, offset)

    # each word must appear in title OR title_en OR content_text (AND across words)
    clauses, params = [], []
    for w in words:
        clauses.append("(title LIKE ? OR title_en LIKE ? OR content_text LIKE ?)")
        like = f"%{w}%"
        params += [like, like, like]
    where = " AND ".join(clauses)

    conn = get_connection()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM articles WHERE {where}", params
    ).fetchone()["c"]
    rows = conn.execute(
        f"""SELECT id, title, title_en, source, day, url,
                   (content_text IS NOT NULL AND content_text != '') AS has_body
            FROM articles WHERE {where}
            ORDER BY day DESC, COALESCE(published,'') DESC, id DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset]
    ).fetchall()
    results = [{
        "id": r["id"], "title": r["title"], "title_en": r["title_en"],
        "source": r["source"], "day": r["day"], "url": r["url"],
        "has_body": bool(r["has_body"]),
    } for r in rows]
    return {"results": results, "total": total}


@app.get("/api/search/body")
def api_search_body(id: int):
    """Fetch one article's stored body on demand (keeps the search page light)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT content_text FROM articles WHERE id=?", (id,)
    ).fetchone()
    return {"body": (row["content_text"] if row else "") or ""}


@app.get("/report", response_class=HTMLResponse)
def report_page():
    """Daily Risk Insight page: preview the brief from today's approved
    articles, then download the .docx once it looks right."""
    from datetime import date as _date
    conn = get_connection()
    today = _date.today().isoformat()
    approved_count = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE day=? AND review_status='approved'",
        (today,)).fetchone()["c"]
    conn.close()

    body = f"""
    <div class="page-head"><div class="eyebrow">Daily Brief</div>
    <h1>Daily Risk Insight</h1>
    <p>Builds the Daily Risk Insights brief from today's <b>approved</b> articles.
       Preview it here, then download the Word document to polish and circulate.</p>
    <hr class="signal-rule"></div>

    <div class="panel">
      <h2>1. Fuel price line</h2>
      <p class="hint">Carried under Macroeconomic Outlook every day. Edit before previewing.</p>
      <input type="text" id="fuelInput" style="width:100%;background:var(--bg-0);
        border:1px solid var(--line);border-radius:8px;padding:10px 12px;color:var(--text);
        font-family:var(--sans);font-size:13.5px;margin-top:6px;"
        value="Fuel Prices: Crude - $70.2, Brent - $72.5, LNG - $3.3">
    </div>

    <div class="panel">
      <h2>2. Generate preview</h2>
      <p class="hint"><b id="approvedCount">{approved_count}</b> article(s) approved today.
         {"Approve some on the Review page first." if approved_count == 0 else "Ready to draft."}</p>
      <div style="display:flex;gap:10px;align-items:center;">
        <button class="btn btn-primary" onclick="previewReport()" id="previewBtn"
          {"disabled" if approved_count == 0 else ""}>Preview brief</button>
        <span id="previewStatus" style="color:var(--gp-3);font-size:13px;"></span>
      </div>
    </div>

    <div class="panel" id="previewPanel" style="display:none;">
      <h2>3. Preview &mdash; review before downloading</h2>
      <p class="hint">Drafted by the LLM in house style. You polish the wording
         after downloading &mdash; this is a starting draft, not a final brief.</p>
      <div id="previewContent" style="background:var(--bg-0);border:1px solid var(--line);
        border-radius:10px;padding:24px 28px;margin-top:10px;"></div>
      <div style="margin-top:16px;display:flex;gap:10px;">
        <button class="btn btn-primary" onclick="downloadReport()" id="downloadBtn">Download .docx</button>
        <button class="btn btn-ghost" onclick="previewReport()">Regenerate</button>
      </div>
    </div>

    <script>
      let _lastFuel = '';
      async function previewReport() {{
        const fuel = document.getElementById('fuelInput').value;
        _lastFuel = fuel;
        const status = document.getElementById('previewStatus');
        const btn = document.getElementById('previewBtn');
        status.textContent = 'Drafting bullets\u2026 (this calls the LLM, may take a moment)';
        btn.disabled = true;
        try {{
          const r = await fetch('/api/report/preview', {{
            method: 'POST', headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{fuel}})
          }});
          const d = await r.json();
          if (!r.ok) {{ status.textContent = d.error || 'Failed to generate preview.'; btn.disabled = false; return; }}
          status.textContent = '';
          btn.disabled = false;
          renderPreview(d);
        }} catch (e) {{
          status.textContent = 'Failed \u2014 is the server running?';
          btn.disabled = false;
        }}
      }}
      function renderPreview(d) {{
        const panel = document.getElementById('previewPanel');
        const c = document.getElementById('previewContent');
        let html = `<div style="font-weight:700;font-size:20px;color:var(--text);margin-bottom:4px;">Daily Risk Insights | ${{d.date}}</div>`;
        html += `<hr style="border:none;border-top:2px solid var(--gp-3);margin:10px 0 18px;">`;
        for (const sec of d.sections) {{
          html += `<div style="font-weight:700;font-size:15px;color:var(--text);margin-top:18px;">${{sec.emoji}} ${{sec.section}}</div>`;
          if (sec.fuel_line) {{
            html += `<div style="color:var(--gp-3);font-weight:600;margin-top:4px;">${{sec.fuel_line}}</div>`;
          }}
          if (sec.bullets.length === 0) {{
            html += `<div style="color:var(--text-3);font-size:13px;margin-top:6px;">No approved articles in this section.</div>`;
          }}
          for (const b of sec.bullets) {{
            const parts = b.split(' \u2013 ');
            let bulletHtml = b;
            if (parts.length > 1) {{
              bulletHtml = `<b>${{parts[0]}}</b> \u2013 ${{parts.slice(1).join(' \u2013 ')}}`;
            }}
            html += `<div style="margin-top:8px;padding-left:18px;position:relative;color:var(--text-2);font-size:13.5px;line-height:1.55;">
              <span style="position:absolute;left:0;">&bull;</span>${{bulletHtml}}</div>`;
          }}
        }}
        if (d.failed && d.failed.length > 0) {{
          html += `<div style="margin-top:22px;padding:14px 16px;background:rgba(226,140,70,.08);
            border:1px solid rgba(226,140,70,.3);border-radius:8px;">
            <div style="color:#e28c46;font-weight:700;font-size:13px;">
              &#9888; Could not fetch full text for ${{d.failed.length}} approved item(s)
            </div>
            <div style="color:var(--text-3);font-size:12px;margin-top:4px;">
              These bullets were drafted from the headline only and may be missing specific figures:
            </div>`;
          for (const f of d.failed) {{
            html += `<div style="margin-top:6px;font-size:12.5px;color:var(--text-2);">
              &bull; ${{f.title}} <span style="color:var(--text-3);">(${{f.source}})</span></div>`;
          }}
          html += `</div>`;
        }}
        c.innerHTML = html;
        panel.style.display = 'block';
        panel.scrollIntoView({{behavior:'smooth', block:'nearest'}});
      }}
      function downloadReport() {{
        window.location.href = '/report/download?fuel=' + encodeURIComponent(_lastFuel);
      }}
    </script>"""
    return page("Daily Risk Insight", body, active="report")


@app.post("/api/report/preview")
async def report_preview(request: Request):
    """Draft the brief from today's approved articles and return it as JSON
    for the in-page preview. Caches the drafted bullets so the subsequent
    download reuses them (no re-drafting, no preview/download drift)."""
    import report as _report
    import llm as _llm
    import json as _json
    from datetime import date as _date
    data = await request.json()
    fuel = (data.get("fuel") or "").strip()

    conn = get_connection()
    today = _date.today().isoformat()
    articles = _approved_articles_today(conn, today)
    if not articles:
        conn.close()
        return JSONResponse(
            {"error": "No approved articles yet - approve some on the Review page first."},
            status_code=400)
    provider = _llm.get_provider()
    display, drafted, failed = _report.preview(articles, provider, fuel_line=fuel)
    # cache for the download step (drafted bullets + which articles failed to fetch)
    cache = _json.dumps({"day": today, "fuel": fuel, "drafted": drafted, "failed": failed})
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('report_cache', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cache,))
    conn.commit()
    conn.close()
    return JSONResponse(display)


@app.get("/report/download")
def report_download(fuel: str = ""):
    """Download the .docx. Reuses the cached drafted bullets from the most
    recent preview if it matches today + this fuel line; otherwise drafts
    fresh (e.g. if the user skipped straight to download)."""
    import report as _report
    import llm as _llm
    import json as _json
    from datetime import date as _date
    conn = get_connection()
    today = _date.today().isoformat()
    fuel = fuel.strip()

    drafted = None
    failed = None
    row = conn.execute("SELECT value FROM settings WHERE key='report_cache'").fetchone()
    if row:
        try:
            cached = _json.loads(row["value"])
            if cached.get("day") == today and cached.get("fuel") == fuel:
                drafted = cached.get("drafted")
                failed = cached.get("failed")
        except Exception:
            drafted = None

    if drafted is None:
        articles = _approved_articles_today(conn, today)
        conn.close()
        if not articles:
            return JSONResponse(
                {"error": "No approved articles yet - approve some on the Review page first."},
                status_code=400)
        provider = _llm.get_provider()
        data, fname = _report.generate(articles, provider, fuel_line=fuel)
    else:
        conn.close()
        data, fname = _report.generate_from_drafted(drafted, fuel_line=fuel, failed=failed)

    out_path = BASE.parent / "data" / fname
    with open(out_path, "wb") as f:
        f.write(data)
    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=fname)


# ── Clear articles ────────────────────────────────────────────────────────────
@app.post("/api/articles/clear")
def clear_articles(scope: str = "today"):
    """Delete fetched articles. scope='all' wipes everything; scope='today'
    clears only today's. Used by the Clear button (which confirms first)."""
    from datetime import date
    conn = get_connection()
    if scope == "all":
        conn.execute("DELETE FROM articles")
    else:
        conn.execute("DELETE FROM articles WHERE day=?", (date.today().isoformat(),))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    return {"cleared": scope, "remaining": n}


# ── Keyword refinement: learn from approved vs rejected headlines ──────────────
@app.get("/keywords/refine", response_class=HTMLResponse)
def keyword_refine_page():
    """Surface candidate keywords: words that appear often in APPROVED headlines
    but aren't keywords yet. You decide whether to add them - the tool gets
    sharper the more you review."""
    import re
    from collections import Counter

    conn = get_connection()
    approved = conn.execute(
        "SELECT title FROM articles WHERE review_status='approved'").fetchall()
    rejected = conn.execute(
        "SELECT title FROM articles WHERE review_status='rejected'").fetchall()
    kw_rows = conn.execute(
        "SELECT term_en, term_bn FROM keywords WHERE enabled=1").fetchall()
    grow = conn.execute(
        "SELECT value FROM settings WHERE key='relevance_guidance'").fetchone()
    guidance = (grow["value"] if grow else "") or ""
    erow = conn.execute(
        "SELECT value FROM settings WHERE key='relevance_examples'").fetchone()
    from llm import DEFAULT_EXAMPLES
    examples = (erow["value"] if erow and erow["value"] else DEFAULT_EXAMPLES)
    conn.close()

    # existing keyword terms (lowercased) to exclude from suggestions
    existing = set()
    for r in kw_rows:
        if (r["term_en"] or "").strip():
            existing.add(r["term_en"].strip().lower())
        for bn in (r["term_bn"] or "").split(","):
            if bn.strip():
                existing.add(bn.strip().lower())

    # very small English stopword list (Bengali handled separately by frequency)
    stop = set("the a an and or of to in on for at by with from as is are was were "
               "be been being this that these those it its his her their our your "
               "new over after before amid says said will can could would should may "
               "two three new amid up down out off into about not no yes than then "
               "bd more most less least also other into per via".split())

    def words(rows):
        c = Counter()
        for r in rows:
            for w in re.findall(r"[A-Za-z\u0980-\u09FF]{3,}", (r["title"] or "")):
                wl = w.lower()
                if wl in stop or wl in existing:
                    continue
                c[wl] += 1
        return c

    appr_c = words(approved)
    rej_c = words(rejected)

    # candidates: frequent in approved, rare in rejected
    candidates = []
    for word, a_count in appr_c.most_common(60):
        r_count = rej_c.get(word, 0)
        if a_count >= 2 and a_count >= r_count:   # signal, not noise
            candidates.append((word, a_count, r_count))

    rows_html = ""
    for word, a_count, r_count in candidates[:30]:
        is_bn = any("\u0980" <= ch <= "\u09FF" for ch in word)
        lang = "BN" if is_bn else "EN"
        rows_html += f"""
        <tr>
          <td class="name">{word}</td>
          <td><span class="lang-tag">{lang}</span></td>
          <td>{a_count}</td>
          <td>{r_count}</td>
          <td><button class="mini approve" onclick="addKeyword('{word}','{lang}')">+ add</button></td>
        </tr>"""

    if not candidates:
        rows_html = ('<tr><td colspan="5" style="color:var(--text-2);padding:20px;">'
                     'No candidates yet. Approve more articles and check back - '
                     'suggestions appear as patterns emerge.</td></tr>')

    body = f"""
    <div class="page-head">
      <div class="eyebrow">Refinement</div>
      <h1>Refine relevance</h1>
      <p>Tune how the AI judges relevance, and grow your keywords from what you
         approve. Based on {len(approved)} approved and {len(rejected)} rejected headlines.</p>
      <hr class="signal-rule">
    </div>

    <div class="panel">
      <h2>AI relevance guidance</h2>
      <p class="hint">Extra instructions added to the AI's relevance scoring prompt.
         The core editorial rules (telecom priority, oil/geopolitics, exclude sports
         etc.) are already built in - this box is for <b>additions or overrides</b>
         in your own words. Applies on the next AI run.</p>
      <textarea id="guidance" rows="4" style="width:100%;background:var(--bg-0);
        border:1px solid var(--line-2);border-radius:8px;padding:12px;color:var(--text);
        font-family:var(--sans);font-size:14px;line-height:1.5;">{guidance}</textarea>
      <div style="margin-top:12px;display:flex;gap:10px;">
        <button class="btn btn-primary" onclick="saveGuidance()">Save guidance</button>
        <span id="gsaved" style="color:var(--gp-3);font-size:13px;align-self:center;"></span>
      </div>
    </div>

    <div class="panel">
      <h2>Scoring examples</h2>
      <p class="hint">Few-shot examples that teach the AI your scoring standard,
         one per line as <code>headline =&gt; score</code> (1–10). These strongly
         shape how the model scores. Edit to match your judgment.</p>
      <textarea id="examples" rows="8" style="width:100%;background:var(--bg-0);
        border:1px solid var(--line-2);border-radius:8px;padding:12px;color:var(--text);
        font-family:var(--mono);font-size:12.5px;line-height:1.6;">{examples}</textarea>
      <div style="margin-top:12px;display:flex;gap:10px;">
        <button class="btn btn-primary" onclick="saveExamples()">Save examples</button>
        <span id="esaved" style="color:var(--gp-3);font-size:13px;align-self:center;"></span>
      </div>
    </div>

    <div class="panel">
      <h2>Learn from my decisions</h2>
      <p class="hint">Analyse your <b>approved</b> and <b>rejected</b> headlines and
         <b>suggest</b> a refinement to the guidance above. Nothing changes until
         you review the suggestion and click Apply - you stay in control.</p>
      <div style="display:flex;gap:10px;">
        <button class="btn btn-primary" onclick="suggestGuidance()">Suggest refinement</button>
        <span id="suggesting" style="color:var(--gp-3);font-size:13px;align-self:center;"></span>
      </div>
      <div id="suggestionBox" style="display:none;margin-top:14px;">
        <p class="hint">Suggested addition to your guidance (review before applying):</p>
        <textarea id="suggestion" rows="5" style="width:100%;background:var(--bg-0);
          border:1px solid var(--gp);border-radius:8px;padding:12px;color:var(--text);
          font-family:var(--sans);font-size:13.5px;line-height:1.5;"></textarea>
        <div style="margin-top:10px;display:flex;gap:10px;">
          <button class="btn btn-primary" onclick="applySuggestion()">Apply to guidance</button>
          <button class="btn" onclick="document.getElementById('suggestionBox').style.display='none'">Discard</button>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>Candidate keywords</h2>
      <p class="hint">Words frequent in your <b>approved</b> headlines but not yet
         keywords. High approved / low rejected = strong signal worth adding.</p>
      <table class="dlist">
        <tr><th>Word</th><th>Lang</th><th>In approved</th><th>In rejected</th><th></th></tr>
        {rows_html}
      </table>
    </div>
    <script>
      async function addKeyword(word, lang) {{
        const section = prompt("Add '" + word + "' to which section?\\n(Geopolitical, Macroeconomy, Political, Telco/regulatory, Others)", "Others");
        if (!section) return;
        const r = await fetch('/api/keywords/add', {{
          method:'POST', headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{word, lang, section}})
        }});
        if (r.ok) location.reload();
      }}
      async function saveGuidance() {{
        const value = document.getElementById('guidance').value;
        const r = await fetch('/api/settings/relevance_guidance', {{
          method:'POST', headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{value}})
        }});
        document.getElementById('gsaved').textContent = r.ok ? '✓ Saved' : 'Save failed';
        setTimeout(()=>{{document.getElementById('gsaved').textContent='';}}, 3000);
      }}
      async function saveExamples() {{
        const value = document.getElementById('examples').value;
        const r = await fetch('/api/settings/relevance_examples', {{
          method:'POST', headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{value}})
        }});
        document.getElementById('esaved').textContent = r.ok ? '✓ Saved' : 'Save failed';
        setTimeout(()=>{{document.getElementById('esaved').textContent='';}}, 3000);
      }}
      async function suggestGuidance() {{
        const s = document.getElementById('suggesting');
        s.textContent = 'Analysing your decisions…';
        try {{
          const r = await fetch('/api/refine/suggest', {{method:'POST'}});
          const d = await r.json();
          s.textContent = '';
          if (d.suggestion) {{
            document.getElementById('suggestion').value = d.suggestion;
            document.getElementById('suggestionBox').style.display = 'block';
          }} else {{
            s.textContent = d.error || 'No suggestion (need more decisions, or LLM off).';
          }}
        }} catch(e) {{ s.textContent = 'Failed - is the LLM running?'; }}
      }}
      async function applySuggestion() {{
        const add = document.getElementById('suggestion').value.trim();
        if (!add) return;
        const box = document.getElementById('guidance');
        box.value = (box.value.trim() + '\\n' + add).trim();
        await saveGuidance();
        document.getElementById('suggestionBox').style.display = 'none';
      }}
    </script>"""
    return page("Refine", body, active="refine")


@app.post("/api/refine/suggest")
async def refine_suggest():
    """SAFE auto-improve: analyse the user's approved/rejected headlines and
    SUGGEST a guidance refinement. Returns text only - never changes anything;
    the user reviews and applies it manually on the Refine page."""
    import llm as _llm
    provider = _llm.get_provider()
    if not getattr(provider, "enabled", False):
        return JSONResponse({"error": "LLM is off - start with LLM_PROVIDER=ollama."})
    conn = get_connection()
    try:
        today = date.today().isoformat()
        approved = [r["title"] for r in conn.execute(
            "SELECT title FROM articles WHERE review_status='approved' "
            "ORDER BY id DESC LIMIT 40").fetchall()]
        rejected = [r["title"] for r in conn.execute(
            "SELECT title FROM articles WHERE review_status='rejected' "
            "ORDER BY id DESC LIMIT 40").fetchall()]
    finally:
        conn.close()
    if len(approved) + len(rejected) < 6:
        return JSONResponse({"error": "Need more approved/rejected decisions first."})
    try:
        suggestion = provider.suggest_guidance(approved, rejected)
        return JSONResponse({"suggestion": suggestion})
    except Exception as e:
        return JSONResponse({"error": f"Could not generate suggestion: {e}"})


@app.post("/api/keywords/add")
async def add_keyword(request: Request):
    """Add a candidate keyword to the database (English or Bengali column)."""
    data = await request.json()
    word = (data.get("word") or "").strip()
    lang = data.get("lang", "EN")
    section = (data.get("section") or "Others").strip()
    if not word:
        raise HTTPException(status_code=400, detail="empty word")
    conn = get_connection()
    if lang == "BN":
        conn.execute("INSERT INTO keywords (section, term_en, term_bn, enabled) VALUES (?,?,?,1)",
                     (section, "", word))
    else:
        conn.execute("INSERT INTO keywords (section, term_en, term_bn, enabled) VALUES (?,?,?,1)",
                     (section, word, ""))
    conn.commit()
    conn.close()
    return {"added": word, "section": section}


# ── Phase 3: source management API ────────────────────────────────────────────
import json as _json


@app.get("/api/sources/{source_id}")
def get_source(source_id: int):
    conn = get_connection()
    r = conn.execute("SELECT id, name, method, config, language, enabled FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    d = dict(r)
    d["config"] = _json.loads(d["config"] or "{}")
    return d


@app.post("/api/sources/save")
async def save_source(request: Request):
    """Add a new source or update an existing one (if id provided)."""
    data = await request.json()
    name = (data.get("name") or "").strip()
    method = data.get("method", "sitemap")
    config = _json.dumps(data.get("config", {}))
    language = data.get("language", "en")
    sid = data.get("id")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    conn = get_connection()
    if sid:
        conn.execute("UPDATE sources SET name=?, method=?, config=?, language=? WHERE id=?",
                     (name, method, config, language, sid))
    else:
        conn.execute("INSERT INTO sources (name, method, config, language, enabled) VALUES (?,?,?,?,1)",
                     (name, method, config, language))
    conn.commit()
    conn.close()
    return {"saved": name}


@app.post("/api/sources/{source_id}/toggle")
def toggle_source(source_id: int):
    conn = get_connection()
    conn.execute("UPDATE sources SET enabled = 1 - enabled WHERE id=?", (source_id,))
    conn.commit()
    conn.close()
    return {"toggled": source_id}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    conn.commit()
    conn.close()
    return {"deleted": source_id}


@app.post("/api/sources/{source_id}/test")
def test_source(source_id: int):
    """Run a live fetch on one source so you can see if it works before relying
    on it. Returns total fetched, today's count, the method used, or an error."""
    import collector_logic as clog
    conn = get_connection()
    r = conn.execute("SELECT name, method, config FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    src = {"name": r["name"], "method": r["method"]}
    try:
        src.update(_json.loads(r["config"] or "{}"))
    except Exception:
        return {"ok": False, "error": "source config is not valid JSON"}
    try:
        articles, method_used = clog.fetch_articles(src)
        total = len(articles)
        today_n = sum(1 for _, _, dt in articles if clog.is_today(dt) is not False)
        if total == 0:
            return {"ok": False, "count": 0, "total": 0, "method": method_used,
                    "error": "fetched 0 articles (source may be down, blocked, or the URL/method is wrong)"}
        return {"ok": True, "count": today_n, "total": total, "method": method_used}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/sources/{source_id}/diagnose")
def diagnose_source_route(source_id: int):
    """Run step-by-step diagnostics on one source: reachability, ID extraction,
    date parsing, endpoint responses. Shows WHY a source returns 0 articles."""
    import collector_logic as clog
    conn = get_connection()
    r = conn.execute("SELECT name, method, config FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    src = {"name": r["name"], "method": r["method"]}
    try:
        src.update(_json.loads(r["config"] or "{}"))
    except Exception:
        return {"ok": False, "checks": [
            {"step": "config", "ok": False, "detail": "source config is not valid JSON"}]}
    try:
        checks = clog.diagnose_source(src)
        all_ok = all(c["ok"] for c in checks) if checks else False
        return {"ok": all_ok, "method": r["method"], "checks": checks}
    except Exception as e:
        return {"ok": False, "checks": [
            {"step": "diagnose", "ok": False, "detail": f"{type(e).__name__}: {e}"}]}


# ── Phase 3: keywords management ──────────────────────────────────────────────
@app.get("/keywords", response_class=HTMLResponse)
def keywords_page():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, section, term_en, term_bn, enabled FROM keywords ORDER BY section, term_en"
    ).fetchall()
    conn.close()

    from collections import defaultdict
    by_section = defaultdict(list)
    for r in rows:
        by_section[r["section"]].append(r)

    sections_html = ""
    for section, items in sorted(by_section.items()):
        krows = ""
        for k in items:
            status = "on" if k["enabled"] else "off"
            pill = ('<span class="pill live"><span class="dot"></span>on</span>' if k["enabled"]
                    else '<span class="pill off"><span class="dot"></span>off</span>')
            krows += f"""
            <tr>
              <td class="name">{k['term_en'] or '-'}</td>
              <td style="font-family:var(--sans);">{k['term_bn'] or '-'}</td>
              <td>{pill}</td>
              <td class="row-actions">
                <button class="mini" onclick="toggleKw({k['id']})">{('disable' if k['enabled'] else 'enable')}</button>
                <button class="mini reject" onclick="delKw({k['id']},'{(k['term_en'] or k['term_bn'])[:20]}')">delete</button>
              </td>
            </tr>"""
        sections_html += f"""
        <div class="panel">
          <h2>{section} <span style="color:var(--text-3);font-size:14px;font-weight:400;">· {len(items)}</span></h2>
          <table class="dlist">
            <tr><th>English</th><th>Bengali</th><th>Status</th><th>Actions</th></tr>
            {krows}
          </table>
        </div>"""

    body = f"""
    <div class="page-head"><div class="eyebrow">Configuration</div>
    <h1>Keywords</h1><p>{len(rows)} keyword rows across {len(by_section)} sections.
    Add, edit, enable/disable - changes apply to the next collection (use
    <b>rematch</b> to re-check already-collected articles).</p>
    <hr class="signal-rule"></div>

    <div class="panel">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h2 style="margin:0;">Add a keyword</h2>
        <button class="btn btn-primary" onclick="openAddKw()">+ Add keyword</button>
      </div>
    </div>
    {sections_html}

    <div id="kwModal" class="modal-bg" style="display:none;">
      <div class="modal">
        <h2>Add keyword</h2>
        <label>Section</label>
        <select id="k_section">
          <option>Geopolitical</option><option>Macroeconomy</option>
          <option>Political</option><option>Telco/regulatory</option><option>Others</option>
        </select>
        <label>English term <span class="lbl-hint">(optional if Bengali given)</span></label>
        <input id="k_en" placeholder="e.g. spectrum">
        <label>Bengali term(s) <span class="lbl-hint">comma-separated, optional</span></label>
        <input id="k_bn" placeholder="e.g. স্পেকট্রাম">
        <div class="modal-actions">
          <button class="btn btn-ghost" onclick="document.getElementById('kwModal').style.display='none'">Cancel</button>
          <button class="btn btn-primary" onclick="saveKw()">Save</button>
        </div>
      </div>
    </div>
    {_sources_script()}
    <script>
      function openAddKw() {{ document.getElementById('kwModal').style.display='flex'; }}
      async function saveKw() {{
        const payload = {{ section: document.getElementById('k_section').value,
          term_en: document.getElementById('k_en').value.trim(),
          term_bn: document.getElementById('k_bn').value.trim() }};
        if (!payload.term_en && !payload.term_bn) {{ alert('Enter an English or Bengali term'); return; }}
        const r = await fetch('/api/keywords/save', {{method:'POST',
          headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(payload)}});
        if (r.ok) location.reload();
      }}
      async function toggleKw(id) {{
        const r = await fetch('/api/keywords/' + id + '/toggle', {{method:'POST'}});
        if (r.ok) location.reload();
      }}
      async function delKw(id, term) {{
        if (!confirm('Delete keyword "' + term + '"?')) return;
        const r = await fetch('/api/keywords/' + id, {{method:'DELETE'}});
        if (r.ok) location.reload();
      }}
    </script>"""
    return page("Keywords", body, active="keywords")


@app.post("/api/keywords/save")
async def save_keyword(request: Request):
    data = await request.json()
    section = (data.get("section") or "Others").strip()
    term_en = (data.get("term_en") or "").strip()
    term_bn = (data.get("term_bn") or "").strip()
    if not term_en and not term_bn:
        raise HTTPException(status_code=400, detail="need a term")
    conn = get_connection()
    conn.execute("INSERT INTO keywords (section, term_en, term_bn, enabled) VALUES (?,?,?,1)",
                 (section, term_en, term_bn))
    conn.commit()
    conn.close()
    return {"saved": term_en or term_bn}


@app.post("/api/keywords/{kw_id}/toggle")
def toggle_keyword(kw_id: int):
    conn = get_connection()
    conn.execute("UPDATE keywords SET enabled = 1 - enabled WHERE id=?", (kw_id,))
    conn.commit()
    conn.close()
    return {"toggled": kw_id}


@app.delete("/api/keywords/{kw_id}")
def delete_keyword(kw_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM keywords WHERE id=?", (kw_id,))
    conn.commit()
    conn.close()
    return {"deleted": kw_id}


# ── settings (editable config like LLM relevance guidance) ────────────────────
@app.post("/api/settings/{key}")
async def save_setting(key: str, request: Request):
    """Store a setting value (e.g. the LLM relevance guidance from Refine)."""
    data = await request.json()
    value = data.get("value", "")
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))
    conn.commit()
    conn.close()
    return {"saved": key}
