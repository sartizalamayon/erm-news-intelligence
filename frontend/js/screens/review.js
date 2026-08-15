// Review queue — the core triage tool (frontend-contract.md §3.3).
//
// REVIEW-REBUILD (perf + UX rebuild): see app/api/routers/review.py's
// module docstring for the measured performance diagnosis (was: 1.77s
// backend time / 131.7MB JSON for one GET /articles at realistic scale
// — one over-fetching request, not literal N+1 — now: ~110ms / ~105KB
// for the same view). This file was rewritten alongside it:
//   - List rows are lightweight (id/title/source/date/time/section/
//     score/dupCount/isStarred/status) and paginated (`limit`/`offset`/
//     `top_n`) — no body text, no dup/topic detail, until a card is
//     actually opened.
//   - Selecting a card fetches full detail on demand via the new
//     GET /articles/{id}, merged into that item in place — one request
//     per user click, not one giant request carrying every row's body.
//   - Bulk approve/reject/star update the already-fetched `s.items`
//     array in place after the single batched POST succeeds, instead of
//     re-fetching (and re-rendering) the whole list.
//   - The "leave a note" feature and every keyboard shortcut (J/K/A/R/
//     X/S//) are gone per the owner's explicit redesign brief — pure
//     checkbox+toolbar interaction, no hidden hotkeys.
//   - The list is date-grouped (newest first), filters gained a date
//     range, an AI-score min–max slider, a "top N by score" mode, and a
//     first-class "Approved only" view distinct from "All".
//
// API (app/api/routers/review.py):
//   GET /articles?q=&sort=&lang=&exclude_source=&status=&starred=&
//                 score_min=&score_max=&date_from=&date_to=&top_n=&
//                 limit=&offset= ->
//     { items: [{ id, title, source, date, time, section, score,
//                 dupCount, isStarred, status, titleEn, url, reason,
//                 keywords: [], body, bodyFetched: bool, note,
//                 dups: [{id,source,title,url}]|null,
//                 topicPeers: [{id,source,title,score}]|null, topic }],
//       total, sources: [{name, count}] }
//     (list rows carry only the first group of fields — body/dups/
//     topicPeers/note/titleEn/url/reason/keywords come back empty/null
//     until fetched via the detail endpoint below.)
//   GET   /articles/{id}            -> full detail for one article
//   PATCH /articles/{id}            { status?, starred?, note? }
//   POST  /articles/bulk            { ids: [], action: "approve"|"reject"|"star" }
//   POST  /articles/{id}/dups/{dupId}/keep
//   POST  /articles/{id}/dups/{dupId}/drop
//
// Tier coloring (score -> High/Medium/Low/Least) is computed
// client-side from the raw score, per §3.3's Server/Client split — the
// server sends the score, not the label.

import { api, ApiError } from "../api.js";
import { escapeHtml, tier, debounce, fmtErr, formatNumber, skeletonRows, notWiredBanner } from "../util.js";

const PAGE_SIZE = 200;
const TOP_N_OPTIONS = [20, 50, 100, 200];
const VIEW_MODES = [
  ["pending", "Pending"],
  ["approved", "Approved"],
  ["all", "All"],
];

export function mountReview(root, { navigate }) {
  let alive = true;
  const s = {
    q: "",
    // Newest-first by default. Relevance ordering interleaves days, so
    // the date grouping (and its counts) read as noise; sorting by date
    // makes the groups mean something. Relevance is one click away.
    sort: "date",
    scoreMin: 1,
    scoreMax: 10,
    dateFrom: "",
    dateTo: "",
    topN: null,
    lang: "",
    srcOff: [],
    viewMode: "pending",
    starredOnly: false,
    srcMenuOpen: false,
    checked: new Set(),
    selId: null,
    dupOpen: false,
    topicOpen: false,
    items: [],
    sources: [],
    total: 0,
    dayCounts: [], // TRUE per-day totals from the API, not the loaded slice
    collapsedDays: new Set(),
    detailOpen: true, // the reading pane; collapsible so the queue can use the full width
    loading: true,
    loadingMore: false,
    bulkBusy: false,
    error: null,
  };

  const debouncedFetch = debounce(() => fetchList(), 300);

  render();
  fetchList();

  async function fetchList({ append = false } = {}) {
    if (append) s.loadingMore = true;
    else s.loading = true;
    s.error = null;
    render();
    try {
      const data = await api.get("/articles", {
        query: {
          q: s.q || undefined,
          sort: s.topN ? undefined : s.sort,
          lang: s.lang || undefined,
          exclude_source: s.srcOff.join(",") || undefined,
          status: s.viewMode === "all" ? undefined : s.viewMode,
          starred: s.starredOnly ? "true" : undefined,
          score_min: s.scoreMin > 1 ? s.scoreMin : undefined,
          score_max: s.scoreMax < 10 ? s.scoreMax : undefined,
          date_from: s.dateFrom || undefined,
          date_to: s.dateTo || undefined,
          top_n: s.topN || undefined,
          limit: s.topN ? undefined : PAGE_SIZE,
          offset: s.topN ? undefined : append ? s.items.length : 0,
        },
      });
      if (!alive) return;
      const newItems = (data && data.items) || [];
      s.items = append ? s.items.concat(newItems) : newItems;
      s.sources = (data && data.sources) || [];
      s.total = (data && data.total) || 0;
      s.dayCounts = (data && data.days) || [];
      s.loading = false;
      s.loadingMore = false;
      if (s.selId && !s.items.some((a) => a.id === s.selId)) s.selId = null;
      if (!s.selId && s.items.length && !append) s.selId = s.items[0].id;
      render();
      if (s.selId) ensureDetail(s.selId);
    } catch (err) {
      if (!alive) return;
      s.loading = false;
      s.loadingMore = false;
      s.error = err;
      render();
    }
  }

  async function ensureDetail(id) {
    const item = s.items.find((a) => a.id === id);
    if (!item || item._detailLoaded || item._detailLoading) return;
    item._detailLoading = true;
    render();
    try {
      const full = await api.get(`/articles/${id}`);
      if (!alive) return;
      const cur = s.items.find((a) => a.id === id);
      if (cur) Object.assign(cur, full, { _detailLoaded: true, _detailLoading: false });
    } catch (err) {
      const cur = s.items.find((a) => a.id === id);
      if (cur) cur._detailLoading = false;
    }
    render();
  }

  function selectArticle(id) {
    s.selId = id;
    s.dupOpen = false;
    s.topicOpen = false;
    render();
    ensureDetail(id);
  }

  function toggleCheck(id) {
    if (s.checked.has(id)) s.checked.delete(id);
    else s.checked.add(id);
    render();
  }

  function toggleSelectAllShown() {
    const allChecked = s.items.length > 0 && s.items.every((a) => s.checked.has(a.id));
    if (allChecked) s.items.forEach((a) => s.checked.delete(a.id));
    else s.items.forEach((a) => s.checked.add(a.id));
    render();
  }

  function toggleStar(id) {
    const item = s.items.find((a) => a.id === id);
    if (!item) return;
    const next = !item.isStarred;
    item.isStarred = next; // optimistic
    render();
    api.patch(`/articles/${id}`, { starred: next }).catch(() => {
      item.isStarred = !next;
      render();
    });
  }

  function decide(id, status) {
    const list = s.items;
    const i = list.findIndex((a) => a.id === id);
    const item = list[i];
    if (!item) return;
    item.status = status; // optimistic
    const stillVisible = s.viewMode === "all" || s.viewMode === status;
    if (!stillVisible) {
      list.splice(i, 1);
      const next = list[i] || list[i - 1];
      s.selId = next ? next.id : null;
    }
    s.dupOpen = false;
    s.topicOpen = false;
    render();
    if (s.selId) ensureDetail(s.selId);
    api.patch(`/articles/${id}`, { status }).catch((err) => {
      s.error = err;
      fetchList();
    });
  }

  // Bulk approve/reject/star: ONE batched request for however many ids
  // are checked (verified at 50-60 ids ~35-40ms server-side — see
  // app/api/routers/review.py's bulk_action, a single `WHERE id =
  // ANY(...)` UPDATE, not a loop). On success the already-fetched
  // `s.items` array is patched in place — no full-list re-fetch/re-render.
  async function bulk(action) {
    const ids = Array.from(s.checked);
    if (!ids.length || s.bulkBusy) return;
    s.bulkBusy = true;
    render();
    try {
      await api.post("/articles/bulk", { ids, action });
      const idSet = new Set(ids);
      if (action === "star") {
        s.items.forEach((a) => {
          if (idSet.has(a.id)) a.isStarred = true;
        });
      } else {
        const newStatus = action === "approve" ? "approved" : "rejected";
        s.items.forEach((a) => {
          if (idSet.has(a.id)) a.status = newStatus;
        });
        const stillVisible = (a) => s.viewMode === "all" || s.viewMode === a.status;
        s.items = s.items.filter((a) => !idSet.has(a.id) || stillVisible(a));
        if (s.selId && idSet.has(s.selId) && !s.items.some((a) => a.id === s.selId)) {
          s.selId = s.items.length ? s.items[0].id : null;
        }
      }
      s.checked.clear();
      s.bulkBusy = false;
      render();
      if (s.selId) ensureDetail(s.selId);
    } catch (err) {
      s.bulkBusy = false;
      s.error = err;
      render();
    }
  }

  function resetFilters() {
    s.q = "";
    s.scoreMin = 1;
    s.scoreMax = 10;
    s.dateFrom = "";
    s.dateTo = "";
    s.topN = null;
    s.lang = "";
    s.srcOff = [];
    s.starredOnly = false;
    fetchList();
  }

  // Re-rendering replaces the whole subtree, which DESTROYS whatever
  // element currently has focus. Typing in the queue filter called
  // fetchList() -> render(), so the input the user was mid-word in was
  // torn out from under them: focus lost, caret gone, and the field
  // appearing to "not work". Same root cause as the run panel's scroll
  // reset. Identity is captured before the swap and restored after.
  function captureFocus() {
    const el = document.activeElement;
    if (!el || !root.contains(el)) return null;
    const key = el.getAttribute("data-role") || el.id;
    if (!key) return null;
    return {
      key,
      start: typeof el.selectionStart === "number" ? el.selectionStart : null,
      end: typeof el.selectionEnd === "number" ? el.selectionEnd : null,
    };
  }

  function restoreFocus(snap) {
    if (!snap) return;
    const el = root.querySelector(`[data-role="${snap.key}"]`) || root.querySelector(`#${CSS.escape(snap.key)}`);
    if (!el) return;
    el.focus({ preventScroll: true });
    if (snap.start !== null && typeof el.setSelectionRange === "function") {
      try {
        el.setSelectionRange(snap.start, snap.end);
      } catch {
        // Some input types (date, range) reject setSelectionRange.
      }
    }
  }

  function render() {
    const focusSnap = captureFocus();
    const listEl = root.querySelector('[data-role="queue-list"]');
    const listScroll = listEl ? listEl.scrollTop : 0;
    const cur = s.items.find((a) => a.id === s.selId) || null;
    const hasMore = !s.topN && !s.loading && s.items.length < s.total;
    const allShownChecked = s.items.length > 0 && s.items.every((a) => s.checked.has(a.id));

    root.innerHTML = `
      <div class="review-shell">
        <div class="review-toolbar">
          <div class="review-toolbar-row">
            <div class="search-box">
              <span style="color:var(--tx4);font-size:12px;">&#8981;</span>
              <input data-role="queue-filter" placeholder="Filter the queue…" value="${escapeHtml(s.q)}" />
            </div>
            <div class="seg-group">
              ${[
                ["relevance", "Relevance"],
                ["date", "Newest"],
                ["source", "Source"],
              ]
                .map(
                  ([k, l]) =>
                    `<button class="seg-btn ${s.sort === k ? "active" : ""}" data-sort="${k}" ${s.topN ? "disabled" : ""}>${l}</button>`
                )
                .join("")}
            </div>
            <div class="seg-group">
              ${VIEW_MODES.map(
                ([k, l]) => `<button class="seg-btn ${s.viewMode === k ? "active" : ""}" data-view="${k}">${l}</button>`
              ).join("")}
            </div>
            <div class="seg-group">
              ${[
                ["", "All"],
                ["en", "EN"],
                ["bn", "বাং"],
              ]
                .map(([k, l]) => `<button class="seg-btn ${s.lang === k ? "active" : ""}" data-lang="${k}">${l}</button>`)
                .join("")}
            </div>
            <div style="position:relative;">
              <button id="srcMenuBtn" class="chk-btn" style="${s.srcOff.length ? "border-color:var(--ac2);color:var(--actx);" : ""}">${
      s.srcOff.length ? "Sources · " + Math.max(0, s.sources.length - s.srcOff.length) : "Sources"
    } &#9662;</button>
              ${
                s.srcMenuOpen
                  ? `<div class="src-menu">
                <div class="src-menu-actions">
                  <button id="srcAllBtn" class="btn-sm" style="flex:1;">All</button>
                  <button id="srcNoneBtn" class="btn-sm" style="flex:1;">None</button>
                </div>
                <div class="src-menu-list">
                  ${s.sources
                    .map(
                      (src) => `<label class="src-menu-item">
                        <input type="checkbox" data-src="${escapeHtml(src.name)}" ${s.srcOff.indexOf(src.name) < 0 ? "checked" : ""} style="accent-color:var(--ac2);width:14px;height:14px;" />
                        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(src.name)}</span>
                        <span class="mono" style="font-size:10px;color:var(--tx4);">${src.count}</span>
                      </label>`
                    )
                    .join("") || `<div style="font-size:12px;color:var(--tx3);padding:8px;">No sources yet.</div>`}
                </div>
              </div>`
                  : ""
              }
            </div>
            <button id="starredBtn" class="chk-btn" style="${s.starredOnly ? "border-color:var(--amber);color:var(--amber);" : ""}">&#9733; ${
      s.items.filter((a) => a.isStarred).length
    }</button>
            <div class="grow"></div>
            <button id="goExportBtn" class="btn btn-primary" style="height:36px;padding:0 15px;white-space:nowrap;">&#8595; Export approved</button>
          </div>

          <div class="review-toolbar-row" style="padding-top:0;">
            <div class="date-range-group">
              <span class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx4);">Date</span>
              <input type="date" data-role="date-from" class="date-input" value="${escapeHtml(s.dateFrom)}" ${s.topN ? "disabled" : ""} />
              <span style="color:var(--tx4);">to</span>
              <input type="date" data-role="date-to" class="date-input" value="${escapeHtml(s.dateTo)}" ${s.topN ? "disabled" : ""} />
            </div>
            <div class="score-slider-group">
              <span class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx4);">Score</span>
              <div class="score-slider">
                <input type="range" min="1" max="10" step="1" data-role="score-min" value="${s.scoreMin}" />
                <input type="range" min="1" max="10" step="1" data-role="score-max" value="${s.scoreMax}" />
              </div>
              <span class="mono" id="scoreRangeLabel" style="font-size:11px;color:var(--tx2);min-width:38px;">${s.scoreMin} to ${s.scoreMax}</span>
            </div>
            <div class="seg-group" style="padding:0 3px;">
              <span class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx4);padding:0 6px;">Top N</span>
              <select data-role="topn-select" class="topn-select">
                <option value="">Off</option>
                ${TOP_N_OPTIONS.map((n) => `<option value="${n}" ${s.topN === n ? "selected" : ""}>Top ${n}</option>`).join("")}
              </select>
            </div>
            ${
              s.q || s.lang || s.srcOff.length || s.starredOnly || s.dateFrom || s.dateTo || s.topN || s.scoreMin > 1 || s.scoreMax < 10
                ? `<button id="clearFiltersBtn" class="btn-ghost" style="font-size:12px;">Reset filters</button>`
                : ""
            }
          </div>

          ${
            s.checked.size
              ? `<div class="bulk-bar">
              <span style="font-size:13px;font-weight:600;color:var(--actx);">${s.checked.size} selected</span>
              <button id="bulkApprove" class="btn btn-primary" style="padding:7px 14px;" ${s.bulkBusy ? "disabled" : ""}>&#10003; Approve all</button>
              <button id="bulkReject" class="btn btn-danger-outline" style="padding:7px 14px;" ${s.bulkBusy ? "disabled" : ""}>&#10005; Reject all</button>
              <button id="bulkStar" class="btn-sm" style="padding:7px 14px;" ${s.bulkBusy ? "disabled" : ""}>&#9733; Star all</button>
              <div class="grow"></div>
              ${s.bulkBusy ? `<span class="mono" style="font-size:11px;color:var(--tx3);">working…</span>` : ""}
              <button id="clearChecked" class="btn-ghost" style="cursor:pointer;font-size:12px;" ${s.bulkBusy ? "disabled" : ""}>Clear selection</button>
            </div>`
              : ""
          }
        </div>

        ${s.error ? errorRow(s.error) : ""}

        <div class="review-body">
          <div class="review-list-pane" data-role="queue-list">
            <div class="review-list-head">
              <label style="display:flex;align-items:center;gap:9px;cursor:pointer;">
                <div class="article-check ${allShownChecked ? "checked" : ""}" data-select-all><span>&#10003;</span></div>
                <span class="mono" style="font-size:10px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);">${
                  s.loading ? "Loading…" : `${formatNumber(s.items.length)} of ${formatNumber(s.total)}`
                }</span>
              </label>
            </div>
            ${renderList()}
            ${
              hasMore
                ? `<div style="padding:14px 18px;text-align:center;"><button id="loadMoreBtn" class="btn btn-outline" ${s.loadingMore ? "disabled" : ""}>${s.loadingMore ? "Loading…" : "Load more"}</button></div>`
                : ""
            }
            ${s.items.length && !s.loading && !hasMore ? `<div class="end-of-queue">end of queue</div>` : ""}
          </div>
          ${
            s.detailOpen
              ? `<div class="review-detail-pane" data-role="detail-pane">
                   <div style="display:flex;justify-content:flex-end;padding:8px 12px 0;">
                     <button id="collapseDetailBtn" class="btn-ghost" title="Collapse the reading pane"
                             aria-label="Collapse the reading pane" style="font-size:12px;">&#10005; Hide</button>
                   </div>
                   ${renderDetail(cur)}
                 </div>`
              : `<button id="expandDetailBtn" class="btn-ghost" title="Show the reading pane"
                         aria-label="Show the reading pane"
                         style="align-self:flex-start;margin:10px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;">
                   &#8592; Show article
                 </button>`
          }
        </div>
      </div>
    `;
    const collapseDetail = root.querySelector("#collapseDetailBtn");
    if (collapseDetail) collapseDetail.addEventListener("click", () => { s.detailOpen = false; render(); });
    const expandDetail = root.querySelector("#expandDetailBtn");
    if (expandDetail) expandDetail.addEventListener("click", () => { s.detailOpen = true; render(); });

    root.querySelectorAll("[data-toggle-day]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const day = btn.getAttribute("data-toggle-day");
        if (s.collapsedDays.has(day)) s.collapsedDays.delete(day);
        else s.collapsedDays.add(day);
        render();
      });
    });
    bind();
    restoreFocus(focusSnap);
    const newList = root.querySelector('[data-role="queue-list"]');
    // Without this the list jumps to the top on every poll/patch —
    // losing your place mid-triage.
    if (newList && listScroll) newList.scrollTop = listScroll;
  }

  function errorRow(err) {
    if (err instanceof ApiError && err.kind === "not_found") {
      return `<div style="padding:12px 26px 0;">${notWiredBanner("GET /articles")}</div>`;
    }
    return `<div style="padding:12px 26px 0;"><div class="banner banner-error"><span>&#10005;</span><div style="flex:1">${escapeHtml(
      fmtErr(err)
    )}</div><button id="reviewRetry" class="btn-ghost" style="color:var(--red);text-decoration:underline;">Retry</button></div></div>`;
  }

  // Groups the already-server-sorted `s.items` into date buckets, newest
  // date first, preserving each item's position within its own date
  // (so whatever sort mode is active — relevance/date/source — still
  // governs ordering INSIDE a day; grouping is a display layer on top).
  function groupByDate(items) {
    const order = [];
    const buckets = new Map();
    for (const a of items) {
      const key = a.date || "";
      if (!buckets.has(key)) {
        buckets.set(key, []);
        order.push(key);
      }
      buckets.get(key).push(a);
    }
    return order.slice().sort((x, y) => (x < y ? 1 : x > y ? -1 : 0)).map((key) => ({ date: key, items: buckets.get(key) }));
  }

  function fmtDateHeader(iso) {
    if (!iso) return "Undated";
    const [y, m, d] = iso.split("-").map(Number);
    if (!y || !m || !d) return iso;
    return new Date(y, m - 1, d).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }

  function renderList() {
    if (s.loading) {
      return `<div style="padding:13px 18px;">${skeletonRows(8, 46)}</div>`;
    }
    if (s.error) return "";
    if (!s.items.length) {
      return `<div class="state-block">
        <div class="state-title">Nothing matches those filters</div>
        <p class="state-body">Loosen the score, date, language or source filters to see more of the matched stories.</p>
        <button id="resetFiltersBtn" class="btn btn-outline">Reset filters</button>
      </div>`;
    }
    // `trueCount` comes from the API's per-day aggregate, NOT from the
    // rows on this page. The header used to print `group.items.length`,
    // so a 200-row page sorted by relevance showed "42" above a day
    // that actually held 543 matched articles — a number that looked
    // like a fact about the day and was really a fact about paging.
    const trueCounts = new Map((s.dayCounts || []).map((d) => [d.date, d.count]));
    return groupByDate(s.items)
      .map((group) => {
        const collapsed = s.collapsedDays.has(group.date);
        const trueCount = trueCounts.get(group.date);
        const loaded = group.items.length;
        // Show "42 of 543" whenever the page holds only part of a day,
        // so the gap is visible instead of silently misleading.
        const countLabel =
          trueCount !== undefined && trueCount > loaded
            ? `${formatNumber(loaded)} of ${formatNumber(trueCount)}`
            : formatNumber(trueCount ?? loaded);
        return `
        <div class="date-group">
          <button class="date-group-header" data-toggle-day="${escapeHtml(group.date)}"
                  aria-expanded="${collapsed ? "false" : "true"}"
                  style="width:100%;background:none;border:0;cursor:pointer;font:inherit;color:inherit;text-align:left;">
            <span style="display:inline-flex;align-items:center;gap:7px;">
              <span style="display:inline-block;width:9px;transition:transform .15s;transform:rotate(${collapsed ? -90 : 0}deg);">&#9662;</span>
              ${escapeHtml(fmtDateHeader(group.date))}
            </span>
            <span class="mono" style="font-size:10px;color:var(--tx4);">${escapeHtml(countLabel)}</span>
          </button>
          ${collapsed ? "" : group.items.map((a) => articleRowHtml(a)).join("")}
        </div>`;
      })
      .join("");
  }

  function articleRowHtml(a) {
    const t = tier(a.score);
    const selected = a.id === s.selId;
    const checked = s.checked.has(a.id);
    const statusLabel = a.status === "approved" ? "Approved" : a.status === "rejected" ? "Rejected" : "";
    const statusBg = a.status === "approved" ? "var(--acsoft)" : "var(--redsoft)";
    const statusFg = a.status === "approved" ? "var(--actx)" : "var(--red)";
    return `<div class="article-row ${selected ? "selected" : ""} ${a.status === "rejected" ? "rejected" : ""}" data-open="${a.id}">
      <div class="article-check ${checked ? "checked" : ""}" data-check="${a.id}"><span>&#10003;</span></div>
      <div style="flex:1;min-width:0;">
        <div class="article-meta-row">
          <span class="tag">${escapeHtml(a.source)}</span>
          <span class="pill" style="background:${t.bg};color:${t.color}"><span class="pill-dot" style="background:${t.color}"></span>${t.label} · ${a.score ?? " to "}</span>
          ${a.dupCount ? `<span class="pill" style="background:var(--acsoft);color:var(--actx);">+${a.dupCount}</span>` : ""}
          ${a.isStarred ? `<span style="font-size:11px;color:var(--amber);">&#9733;</span>` : ""}
          ${statusLabel ? `<span class="pill" style="background:${statusBg};color:${statusFg};text-transform:uppercase;letter-spacing:.6px;font-size:9px;">${statusLabel}</span>` : ""}
        </div>
        <div class="article-title">${escapeHtml(a.title)}</div>
        <div class="article-sub">${escapeHtml(a.time || "")} · ${escapeHtml(a.section || "")}</div>
      </div>
    </div>`;
  }

  function renderDetail(cur) {
    if (s.loading) return `<div style="padding:24px 30px;">${skeletonRows(10, 18)}</div>`;
    if (!cur) {
      return `<div style="height:100%;display:flex;align-items:center;justify-content:center;padding:40px;">
        <div style="text-align:center;max-width:34ch;">
          <div class="state-title">Pick a story</div>
          <p class="state-body">Select anything in the queue to read it, see why it scored the way it did, and approve or reject.</p>
        </div>
      </div>`;
    }
    const t = tier(cur.score);
    const detailReady = !!cur._detailLoaded;
    const dups = cur.dups || null;
    const peers = cur.topicPeers || null;
    return `<div class="detail-wrap">
      <div class="detail-head">
        <span class="tag" style="font-size:12px;padding:4px 11px;">${escapeHtml(cur.source)}</span>
        <span class="pill" style="background:${t.bg};color:${t.color};font-size:11px;padding:4px 10px;"><span class="pill-dot" style="background:${t.color}"></span>${t.label} · ${cur.score ?? "n/a"}/10</span>
        <span class="mono" style="font-size:11px;color:var(--tx2);">${escapeHtml(cur.section || "")}</span>
        <div class="grow"></div>
        <button id="starCurBtn" class="icon-btn" title="Star for later" style="color:${cur.isStarred ? "var(--amber)" : "var(--tx4)"};border-color:${cur.isStarred ? "var(--amber)" : "var(--line)"}">&#9733;</button>
        <span class="mono" style="font-size:11px;color:var(--tx3);">${escapeHtml(cur.time || "")}</span>
      </div>
      <h2 class="detail-title">${escapeHtml(cur.title)}</h2>
      ${detailReady && cur.titleEn ? `<div class="detail-title-en">${escapeHtml(cur.titleEn)}</div>` : ""}
      ${detailReady ? `<a href="${escapeHtml(cur.url || "#")}" target="_blank" rel="noopener noreferrer" style="display:inline-flex;align-items:center;gap:6px;font-size:12.5px;margin-bottom:20px;">Open at source &#8599;</a>` : ""}
      <div class="detail-actions">
        <button id="approveCurBtn" class="btn btn-primary" style="flex:1;justify-content:center;padding:13px 20px;font-size:14px;">&#10003; Approve</button>
        <button id="rejectCurBtn" class="btn btn-outline" style="flex:1;justify-content:center;padding:13px 20px;font-size:14px;">&#10005; Reject</button>
      </div>
      ${
        !detailReady
          ? `<div style="padding:8px 0;">${skeletonRows(6, 16)}</div>`
          : `
      <div class="reason-box" style="border-left-color:${t.color}">
        <div class="reason-kicker">Why it scored ${cur.score ?? "n/a"}</div>
        <div style="font-size:13.5px;color:var(--tx2);line-height:1.6;">${escapeHtml(cur.reason || "")}</div>
        <div class="row wrap" style="gap:7px;margin-top:11px;">${(cur.keywords || []).map((k) => `<span class="kw-chip">${escapeHtml(k)}</span>`).join("")}</div>
      </div>
      ${
        dups && dups.length
          ? `<div style="margin-bottom:20px;">
        <button id="toggleDupBtn" class="dup-toggle">
          <span style="color:var(--actx);">${s.dupOpen ? "&#8964;" : "&#8250;"}</span>
          Also reported by ${dups.length} other outlets
          <span class="grow"></span>
          <span class="mono" style="font-size:10.5px;color:var(--tx3);font-weight:400;">keep one version instead</span>
        </button>
        ${
          s.dupOpen
            ? `<div class="stack" style="gap:7px;margin-top:9px;">${dups
                .map(
                  (d) => `<div class="peer-row">
              <span style="flex:0 0 114px;font-size:12px;font-weight:600;color:var(--actx);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(d.source)}</span>
              <a href="${escapeHtml(d.url || "#")}" target="_blank" rel="noopener noreferrer" style="flex:1;min-width:0;font-size:12.5px;color:var(--tx2);">${escapeHtml(d.title)}</a>
              <div class="row" style="gap:6px;flex:0 0 auto;">
                <button class="btn-sm" data-dup-keep="${d.id}">keep</button>
                <button class="btn-sm" data-dup-drop="${d.id}">drop</button>
              </div>
            </div>`
                )
                .join("")}</div>`
            : ""
        }
      </div>`
          : ""
      }
      ${
        peers && peers.length
          ? `<div style="margin-bottom:20px;">
        <button id="toggleTopicBtn" class="topic-toggle">
          <span style="color:var(--amber);">&#9670;</span>
          ${peers.length} more on "${escapeHtml(cur.topic || "")}"
          <span class="grow"></span>
          <span style="color:var(--amber);">${s.topicOpen ? "&#8964;" : "&#8250;"}</span>
        </button>
        ${
          s.topicOpen
            ? `<div class="stack" style="gap:7px;margin-top:9px;">${peers
                .map((p) => {
                  const pt = tier(p.score);
                  return `<div class="peer-row">
                <span style="flex:0 0 auto;width:7px;height:7px;border-radius:50%;background:${pt.color}"></span>
                <span style="flex:0 0 102px;font-size:12px;font-weight:600;color:var(--actx);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(p.source)}</span>
                <a href="${escapeHtml(p.url || "#")}" target="_blank" rel="noopener noreferrer" style="flex:1;min-width:0;font-size:12.5px;color:var(--tx2);">${escapeHtml(p.title)}</a>
                <div class="row" style="gap:6px;flex:0 0 auto;">
                  <button class="btn-sm" data-peer-approve="${p.id}">approve</button>
                  <button class="btn-sm" data-peer-reject="${p.id}">reject</button>
                </div>
              </div>`;
                })
                .join("")}</div>`
            : ""
        }
      </div>`
          : ""
      }
      <div>
        <div class="row" style="gap:10px;margin-bottom:12px;">
          <div class="reason-kicker" style="margin-bottom:0;">Article text</div>
          <div style="flex:1;height:1px;background:var(--line);"></div>
        </div>
        ${
          cur.body
            ? `<div class="article-body-text">${escapeHtml(cur.body)}</div>`
            : `<div class="body-missing"><span style="color:var(--tx3);">·</span><div style="flex:1;font-size:13px;color:var(--tx2);line-height:1.55;">Body text wasn't retrieved for this article. Read it at the source, or ask an administrator to re-run the text fetch.</div></div>`
        }
      </div>`
      }
    </div>`;
  }

  function bind() {
    const filterInput = root.querySelector('[data-role="queue-filter"]');
    if (filterInput) {
      filterInput.addEventListener("input", (e) => {
        s.q = e.target.value;
        debouncedFetch();
      });
    }
    root.querySelectorAll("[data-sort]").forEach((b) =>
      b.addEventListener("click", () => {
        if (s.topN) return;
        s.sort = b.getAttribute("data-sort");
        fetchList();
      })
    );
    root.querySelectorAll("[data-view]").forEach((b) =>
      b.addEventListener("click", () => {
        s.viewMode = b.getAttribute("data-view");
        fetchList();
      })
    );
    root.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => { s.lang = b.getAttribute("data-lang"); fetchList(); }));

    const srcMenuBtn = root.querySelector("#srcMenuBtn");
    if (srcMenuBtn) srcMenuBtn.addEventListener("click", () => { s.srcMenuOpen = !s.srcMenuOpen; render(); });
    root.querySelectorAll("[data-src]").forEach((cb) =>
      cb.addEventListener("change", () => {
        const name = cb.getAttribute("data-src");
        if (cb.checked) s.srcOff = s.srcOff.filter((x) => x !== name);
        else s.srcOff.push(name);
        fetchList();
      })
    );
    const srcAll = root.querySelector("#srcAllBtn");
    if (srcAll) srcAll.addEventListener("click", () => { s.srcOff = []; fetchList(); });
    const srcNone = root.querySelector("#srcNoneBtn");
    if (srcNone) srcNone.addEventListener("click", () => { s.srcOff = s.sources.map((x) => x.name); fetchList(); });

    const starredBtn = root.querySelector("#starredBtn");
    if (starredBtn) starredBtn.addEventListener("click", () => { s.starredOnly = !s.starredOnly; fetchList(); });
    const goExport = root.querySelector("#goExportBtn");
    if (goExport) goExport.addEventListener("click", () => navigate("export"));

    const dateFrom = root.querySelector('[data-role="date-from"]');
    const dateTo = root.querySelector('[data-role="date-to"]');
    if (dateFrom) dateFrom.addEventListener("change", () => { s.dateFrom = dateFrom.value; fetchList(); });
    if (dateTo) dateTo.addEventListener("change", () => { s.dateTo = dateTo.value; fetchList(); });

    const scoreMinEl = root.querySelector('[data-role="score-min"]');
    const scoreMaxEl = root.querySelector('[data-role="score-max"]');
    if (scoreMinEl && scoreMaxEl) {
      const label = root.querySelector("#scoreRangeLabel");
      const sync = () => {
        if (Number(scoreMinEl.value) > Number(scoreMaxEl.value)) scoreMinEl.value = scoreMaxEl.value;
        if (label) label.textContent = `${scoreMinEl.value} to ${scoreMaxEl.value}`;
      };
      scoreMinEl.addEventListener("input", sync);
      scoreMaxEl.addEventListener("input", sync);
      const commit = () => {
        s.scoreMin = Number(scoreMinEl.value);
        s.scoreMax = Number(scoreMaxEl.value);
        fetchList();
      };
      scoreMinEl.addEventListener("change", commit);
      scoreMaxEl.addEventListener("change", commit);
    }

    const topNSelect = root.querySelector('[data-role="topn-select"]');
    if (topNSelect) {
      topNSelect.addEventListener("change", () => {
        s.topN = topNSelect.value ? Number(topNSelect.value) : null;
        fetchList();
      });
    }

    const clearFiltersBtn = root.querySelector("#clearFiltersBtn");
    if (clearFiltersBtn) clearFiltersBtn.addEventListener("click", resetFilters);

    const selectAll = root.querySelector("[data-select-all]");
    if (selectAll) selectAll.addEventListener("click", (e) => { e.stopPropagation(); toggleSelectAllShown(); });

    root.querySelectorAll("[data-open]").forEach((row) =>
      row.addEventListener("click", (e) => {
        if (e.target.closest("[data-check]")) return;
        selectArticle(Number(row.getAttribute("data-open")));
      })
    );
    root.querySelectorAll("[data-check]").forEach((chk) =>
      chk.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleCheck(Number(chk.getAttribute("data-check")));
      })
    );

    const loadMoreBtn = root.querySelector("#loadMoreBtn");
    if (loadMoreBtn) loadMoreBtn.addEventListener("click", () => fetchList({ append: true }));

    const resetBtn = root.querySelector("#resetFiltersBtn");
    if (resetBtn) resetBtn.addEventListener("click", resetFilters);
    const retryBtn = root.querySelector("#reviewRetry");
    if (retryBtn) retryBtn.addEventListener("click", () => fetchList());

    const bulkApprove = root.querySelector("#bulkApprove");
    if (bulkApprove) bulkApprove.addEventListener("click", () => bulk("approve"));
    const bulkReject = root.querySelector("#bulkReject");
    if (bulkReject) bulkReject.addEventListener("click", () => bulk("reject"));
    const bulkStar = root.querySelector("#bulkStar");
    if (bulkStar) bulkStar.addEventListener("click", () => bulk("star"));
    const clearChecked = root.querySelector("#clearChecked");
    if (clearChecked) clearChecked.addEventListener("click", () => { s.checked.clear(); render(); });

    const starCurBtn = root.querySelector("#starCurBtn");
    if (starCurBtn) starCurBtn.addEventListener("click", () => toggleStar(s.selId));
    const approveCurBtn = root.querySelector("#approveCurBtn");
    if (approveCurBtn) approveCurBtn.addEventListener("click", () => decide(s.selId, "approved"));
    const rejectCurBtn = root.querySelector("#rejectCurBtn");
    if (rejectCurBtn) rejectCurBtn.addEventListener("click", () => decide(s.selId, "rejected"));

    const toggleDupBtn = root.querySelector("#toggleDupBtn");
    if (toggleDupBtn) toggleDupBtn.addEventListener("click", () => { s.dupOpen = !s.dupOpen; render(); });
    root.querySelectorAll("[data-dup-keep]").forEach((b) =>
      b.addEventListener("click", () => {
        api.post(`/articles/${s.selId}/dups/${b.getAttribute("data-dup-keep")}/keep`).catch(() => {});
      })
    );
    root.querySelectorAll("[data-dup-drop]").forEach((b) =>
      b.addEventListener("click", () => {
        const cur = s.items.find((a) => a.id === s.selId);
        const dupId = b.getAttribute("data-dup-drop");
        api.post(`/articles/${s.selId}/dups/${dupId}/drop`).catch(() => {});
        if (cur && cur.dups) cur.dups = cur.dups.filter((d) => String(d.id) !== dupId);
        render();
      })
    );

    const toggleTopicBtn = root.querySelector("#toggleTopicBtn");
    if (toggleTopicBtn) toggleTopicBtn.addEventListener("click", () => { s.topicOpen = !s.topicOpen; render(); });
    root.querySelectorAll("[data-peer-approve]").forEach((b) =>
      b.addEventListener("click", () => {
        api.patch(`/articles/${b.getAttribute("data-peer-approve")}`, { status: "approved" }).catch(() => {});
      })
    );
    root.querySelectorAll("[data-peer-reject]").forEach((b) =>
      b.addEventListener("click", () => {
        api.patch(`/articles/${b.getAttribute("data-peer-reject")}`, { status: "rejected" }).catch(() => {});
      })
    );
  }

  return {
    destroy() {
      alive = false;
    },
  };
}
