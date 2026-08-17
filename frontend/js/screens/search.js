// Search archive: every article ever collected.
//
// API (app/api/routers/search.py):
//   GET /search?q=&page=&date_from=&date_to=&lang=&status=&exclude_source= ->
//     { results: [{ id, source, day, title, titleEn, url, status, body }],
//       total, corpusSize, sources: [{name, count}] }
//
// Every word must appear; results are newest first; the server pages at
// a fixed 20 (app/services/search.py PAGE_SIZE).
//
// WHAT THIS REBUILD CHANGES
//
//   1. Results say WHY they matched. Searching 60,000 articles and
//      getting back plain headlines meant re-reading each one to find
//      the word. Title, translation and a body snippet now highlight
//      the query terms, and a result that matched only in the body
//      leads with the snippet rather than hiding it behind an expander.
//
//   2. The pager was wrong on the last page. It derived the page size
//      from `s.results.length`, so a final page of 17 rows computed
//      "page size 17" and therefore ceil(277/17) = 17 pages instead of
//      14. Next stayed enabled past the end and walked into empty pages
//      that rendered as "No matches", which reads as a broken filter.
//      PAGE_SIZE is a server constant and is now treated as one.
//
//   3. Searches are linkable. This is an archive; "send me that search"
//      is the obvious thing to want, and every screen got an address
//      recently except the one where a URL carries the most meaning.
//      Query and filters live in the query string, restored on load,
//      and cleared on the way out so they do not follow you to another
//      screen.
//
//   4. Filter changes are visible before a search runs. Unchecking a
//      source used to mutate state with no re-render, so the control
//      showed the old count until a request came back.
//
//   5. Article links carry rel="noopener noreferrer". The href comes
//      from a scraped page, which is remote input.

import { api } from "../api.js";
import {
  escapeHtml,
  fmtErr,
  formatNumber,
  highlightTerms,
  notWiredBanner,
  searchTerms,
  skeletonRows,
  snippetAround,
} from "../util.js";

// Mirrors app/services/search.py's PAGE_SIZE. A wrong value here does
// not break a request, it breaks the page count, which is exactly the
// bug this rebuild fixes, so it is named rather than inferred.
const PAGE_SIZE = 20;

const SUGGESTIONS = ["hormuz oil", "spectrum auction", "ভ্যাট", "submarine cable"];
const STATUSES = [
  ["", "All"],
  ["matched", "Matched"],
  ["approved", "Approved"],
  ["rejected", "Rejected"],
  ["never_matched", "Never matched"],
];
const LANGS = [
  ["", "All"],
  ["en", "EN"],
  ["bn", "বাং"],
];

/** Query-string keys this screen owns, so it can clean up after itself. */
const URL_KEYS = ["q", "page", "from", "to", "lang", "status", "not"];

export function mountSearch(root) {
  let alive = true;
  const s = {
    q: "",
    ran: false,
    loading: false,
    error: null,
    results: [],
    total: 0,
    corpusSize: null,
    openBodies: new Set(),
    selId: null, // the result open in the reading pane
    detailOpen: true, // collapsible, so the result list can use the full width
    loadingMore: false,
    page: 1,
    dateFrom: "",
    dateTo: "",
    lang: "",
    status: "",
    sources: [],
    srcOff: [],
    srcMenuOpen: false,
  };

  // Mount scope: see armInfiniteScroll's note on the temporal dead zone.
  let listObserver = null;

  readUrl();
  render();
  // A pasted link should show its results, not an empty box with the
  // words already typed in.
  if (s.q.trim()) runSearch({ page: s.page, push: false });

  // ── URL <-> state ────────────────────────────────────────────────────

  function readUrl() {
    if (typeof window === "undefined") return;
    const p = new URLSearchParams(window.location.search);
    s.q = p.get("q") || "";
    s.page = Math.max(1, Number(p.get("page")) || 1);
    s.dateFrom = p.get("from") || "";
    s.dateTo = p.get("to") || "";
    s.lang = LANGS.some(([k]) => k === p.get("lang")) ? p.get("lang") : "";
    s.status = STATUSES.some(([k]) => k === p.get("status")) ? p.get("status") : "";
    s.srcOff = (p.get("not") || "").split(",").filter(Boolean);
  }

  function writeUrl() {
    if (typeof window === "undefined" || !window.history) return;
    const p = new URLSearchParams(window.location.search);
    for (const k of URL_KEYS) p.delete(k);
    if (s.q.trim()) p.set("q", s.q.trim());
    if (s.page > 1) p.set("page", String(s.page));
    if (s.dateFrom) p.set("from", s.dateFrom);
    if (s.dateTo) p.set("to", s.dateTo);
    if (s.lang) p.set("lang", s.lang);
    if (s.status) p.set("status", s.status);
    if (s.srcOff.length) p.set("not", s.srcOff.join(","));
    const qs = p.toString();
    // replaceState, not push: a search refines in place, and pushing
    // every keystroke-driven filter change would make Back walk one
    // filter at a time back through a session's worth of tweaking.
    window.history.replaceState(
      window.history.state,
      "",
      window.location.pathname + (qs ? "?" + qs : "") + window.location.hash
    );
  }

  // ── derived ──────────────────────────────────────────────────────────

  const terms = () => searchTerms(s.q);
  const totalPages = () => Math.max(1, Math.ceil(s.total / PAGE_SIZE));

  function hasActiveFilters() {
    return !!(s.dateFrom || s.dateTo || s.lang || s.status || s.srcOff.length);
  }

  /** Each active filter as a removable chip, so nothing filters silently. */
  function activeFilterChips() {
    const chips = [];
    if (s.dateFrom || s.dateTo) {
      chips.push([`${s.dateFrom || "any"} to ${s.dateTo || "any"}`, "date"]);
    }
    if (s.lang) chips.push([`language: ${(LANGS.find(([k]) => k === s.lang) || [])[1]}`, "lang"]);
    if (s.status) chips.push([`status: ${(STATUSES.find(([k]) => k === s.status) || [])[1]}`, "status"]);
    if (s.srcOff.length) chips.push([`${s.srcOff.length} source${s.srcOff.length === 1 ? "" : "s"} hidden`, "src"]);
    return chips;
  }

  function dateRangeInvalid() {
    return !!(s.dateFrom && s.dateTo && s.dateFrom > s.dateTo);
  }

  function clearFilter(kind) {
    if (kind === "date") { s.dateFrom = ""; s.dateTo = ""; }
    if (kind === "lang") s.lang = "";
    if (kind === "status") s.status = "";
    if (kind === "src") s.srcOff = [];
    refilter();
  }

  function resetFilters() {
    s.dateFrom = "";
    s.dateTo = "";
    s.lang = "";
    s.status = "";
    s.srcOff = [];
    refilter();
  }

  /**
   * A filter changed. Re-search if one has already run, otherwise just
   * redraw so the control reflects the click. The old code did neither
   * in the "not yet searched" case, so the source picker looked frozen.
   */
  function refilter() {
    if (s.ran) runSearch({ page: 1 });
    else {
      writeUrl();
      render();
    }
  }

  // ── search ───────────────────────────────────────────────────────────

  async function runSearch({ page = 1, push = true, append = false } = {}) {
    if (!s.q.trim()) return;
    // An impossible range returns nothing and looks identical to a
    // genuine no-match, so it is refused before the request.
    if (dateRangeInvalid()) {
      s.ran = true;
      render();
      return;
    }
    s.ran = true;
    s.page = Math.max(1, page);
    if (append) s.loadingMore = true;
    else s.loading = true;
    s.error = null;
    if (push) writeUrl();
    render();
    try {
      const data = await api.get("/search", {
        query: {
          q: s.q,
          page: s.page,
          date_from: s.dateFrom || undefined,
          date_to: s.dateTo || undefined,
          lang: s.lang || undefined,
          status: s.status || undefined,
          exclude_source: s.srcOff.join(",") || undefined,
        },
      });
      if (!alive) return;
      const batch = (data && data.results) || [];
      s.results = append ? s.results.concat(batch) : batch;
      s.total = (data && data.total) ?? s.results.length;
      s.corpusSize = data && data.corpusSize;
      // The source list comes back with the results, so it only exists
      // once a search has run. Keep the previous list when a response
      // omits it, or opening the picker after a zero-result search
      // would show nothing to un-hide.
      if (data && data.sources && data.sources.length) s.sources = data.sources;
      s.loading = false;
      s.loadingMore = false;
      if (!append) {
        s.openBodies.clear();
        s.selId = s.results.length ? s.results[0].id : null;
      }
      render();
    } catch (err) {
      if (!alive) return;
      s.loading = false;
      s.loadingMore = false;
      s.error = err;
      render();
    }
  }


  // ── fragments ────────────────────────────────────────────────────────

  /** One row in the results list. Compact, because it is a scan surface. */
  function resultRow(r) {
    const t = terms();
    const sel = s.selId === r.id;
    const statusLabel = r.status === "approved" ? "Approved" : r.status === "rejected" ? "Rejected" : "";
    const statusFg = r.status === "approved" ? "var(--actx)" : "var(--red)";

    // Where did it match? A body-only hit is the case worth flagging:
    // the headline will not contain the word, so the row looks like a
    // false positive until you know why it is here.
    const inTitle = t.some((w) => (r.title || "").toLowerCase().includes(w));
    const inTitleEn = t.some((w) => (r.titleEn || "").toLowerCase().includes(w));
    const inBody = t.some((w) => (r.body || "").toLowerCase().includes(w));
    const bodyOnly = !inTitle && !inTitleEn && inBody;

    return `<div class="article-row ${sel ? "selected" : ""}" data-result="${escapeHtml(String(r.id))}"
                 role="button" tabindex="0" aria-pressed="${sel ? "true" : "false"}">
      <div style="flex:1;min-width:0;">
        <div class="article-meta-row">
          <span class="tag">${escapeHtml(r.source)}</span>
          <span class="mono" style="font-size:10px;color:var(--tx3);">${escapeHtml(r.day)}</span>
          ${statusLabel ? `<span class="mono" style="font-size:9px;letter-spacing:.6px;text-transform:uppercase;color:${statusFg};">${statusLabel}</span>` : ""}
          ${bodyOnly ? `<span class="mono" style="font-size:9px;letter-spacing:.6px;text-transform:uppercase;color:var(--tx4);">in body</span>` : ""}
        </div>
        <div class="article-title">${highlightTerms(r.title, t)}</div>
        ${
          r.titleEn && r.titleEn !== r.title
            ? `<div style="font-size:12px;color:var(--tx2);font-style:italic;margin-top:3px;">${highlightTerms(r.titleEn, t)}</div>`
            : ""
        }
        ${
          r.body
            ? `<div style="margin-top:6px;font-size:11.5px;line-height:1.55;color:var(--tx3);">${highlightTerms(snippetAround(r.body, t, 110), t)}</div>`
            : ""
        }
      </div>
    </div>`;
  }

  /**
   * The reading pane.
   *
   * The archive used to expand an article INLINE, which pushed every
   * result below it down the page and lost your place in a list you
   * were part-way through scanning. Searching is read-one, go-back,
   * read-another; that wants a stable list beside a stable reader,
   * which is exactly the shape the Review queue already uses. Matching
   * it also means one thing to learn instead of two.
   */
  function renderDetail() {
    const r = s.results.find((x) => x.id === s.selId);
    if (!r) {
      return `<div class="detail-wrap"><div class="state-block">
        <div class="state-title">Nothing selected</div>
        <p class="state-body">Pick a result on the left to read it here.</p>
      </div></div>`;
    }
    const t = terms();
    return `<div class="detail-wrap">
      <div class="detail-head">
        <span class="tag">${escapeHtml(r.source)}</span>
        <span class="mono" style="font-size:11px;color:var(--tx3);">${escapeHtml(r.day)}</span>
      </div>
      <h2 class="detail-title">${highlightTerms(r.title, t)}</h2>
      ${r.titleEn && r.titleEn !== r.title ? `<div class="detail-title-en">${highlightTerms(r.titleEn, t)}</div>` : ""}
      <a href="${escapeHtml(r.url || "#")}" target="_blank" rel="noopener noreferrer"
         style="display:inline-block;margin:4px 0 18px;font-size:12.5px;">Open at source &#8599;</a>
      ${
        r.body
          ? `<div style="font-family:'Lora',Georgia,serif;font-size:15px;line-height:1.8;color:var(--tx2);white-space:pre-wrap;">${highlightTerms(r.body, t)}</div>`
          : `<div style="font-size:13px;color:var(--tx3);">No article text was captured for this one. Open it at source to read it.</div>`
      }
    </div>`;
  }


  function filterBar() {
    const visibleSources = Math.max(0, s.sources.length - s.srcOff.length);
    return `<div class="row wrap" style="gap:10px;margin-bottom:12px;align-items:center;">
      <div class="date-range-group ${dateRangeInvalid() ? "range-invalid" : ""}">
        <span class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx4);">Date</span>
        <input type="date" data-role="date-from" class="date-input" value="${escapeHtml(s.dateFrom)}" />
        <span style="color:var(--tx4);">to</span>
        <input type="date" data-role="date-to" class="date-input" value="${escapeHtml(s.dateTo)}" />
      </div>
      <div class="seg-group">
        ${LANGS.map(([k, l]) => `<button class="seg-btn ${s.lang === k ? "active" : ""}" data-lang="${k}">${l}</button>`).join("")}
      </div>
      <div class="seg-group">
        ${STATUSES.map(([k, l]) => `<button class="seg-btn ${s.status === k ? "active" : ""}" data-status="${k}">${l}</button>`).join("")}
      </div>
      <div style="position:relative;">
        <button id="srcMenuBtn" class="chk-btn" style="${s.srcOff.length ? "border-color:var(--ac2);color:var(--actx);" : ""}">${
          s.srcOff.length ? `Sources · ${visibleSources}` : "Sources"
        } &#9662;</button>
        ${
          s.srcMenuOpen
            ? `<div class="src-menu">
          <div class="src-menu-actions">
            <button id="srcAllBtn" class="btn-sm" style="flex:1;">All</button>
            <button id="srcNoneBtn" class="btn-sm" style="flex:1;">None</button>
          </div>
          <div class="src-menu-list">
            ${
              s.sources
                .map(
                  (src) => `<label class="src-menu-item">
                    <input type="checkbox" data-src="${escapeHtml(src.name)}" ${s.srcOff.indexOf(src.name) < 0 ? "checked" : ""} style="accent-color:var(--ac2);width:14px;height:14px;" />
                    <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(src.name)}</span>
                    <span class="mono" style="font-size:10px;color:var(--tx4);">${escapeHtml(String(src.count))}</span>
                  </label>`
                )
                .join("") || `<div style="font-size:12px;color:var(--tx3);padding:8px;">Run a search to see which sources it hit.</div>`
            }
          </div>
        </div>`
            : ""
        }
      </div>
    </div>
    ${
      hasActiveFilters()
        ? `<div class="row wrap" style="gap:7px;margin-bottom:16px;align-items:center;">
        ${activeFilterChips()
          .map(
            ([text, kind]) =>
              `<button class="chip-toggle active" data-clear-filter="${kind}" style="border-radius:999px;" title="Remove this filter">${escapeHtml(text)} &#10005;</button>`
          )
          .join("")}
        <button id="clearFiltersBtn" class="btn-ghost" style="font-size:12px;">Reset all</button>
      </div>`
        : ""
    }`;
  }

  // ── render ───────────────────────────────────────────────────────────

  function render() {
    const listScroller = root.querySelector('[data-role="results-list"]');
    const keepScroll = listScroller ? listScroller.scrollTop : 0;

    root.innerHTML = `
      <div class="split-shell">
        <div class="review-toolbar">
          <div class="review-toolbar-row">
            <div class="search-box" style="max-width:420px;">
              <span style="color:var(--tx4);font-size:12px;">&#8981;</span>
              <input id="searchInput" value="${escapeHtml(s.q)}" placeholder="Search every article ever collected"
                     aria-label="Search the archive" />
              ${s.q ? `<button id="clearQueryBtn" class="btn-ghost" style="color:var(--tx4);padding:0 4px;" aria-label="Clear the search">&#10005;</button>` : ""}
            </div>
            <button id="searchBtn" class="btn btn-primary" style="height:36px;padding:0 18px;">Search</button>
            <div class="grow"></div>
            ${
              s.ran && !s.loading && !s.error
                ? `<span class="mono" style="font-size:11px;color:var(--tx3);">${
                    s.total
                      ? `${formatNumber(s.results.length)} of ${formatNumber(s.total)} matches`
                      : "no matches"
                  }${s.corpusSize ? ` &#183; ${formatNumber(s.corpusSize)} articles searched` : ""}</span>`
                : ""
            }
          </div>
          <div class="review-toolbar-row" style="padding-top:0;">
            ${filterBar()}
          </div>
        </div>

        ${
          dateRangeInvalid()
            ? `<div class="banner banner-warn" style="margin:12px 26px;"><span>&#9888;</span><div style="flex:1;">The start date is after the end date, so nothing can match. Swap them or clear one.</div></div>`
            : ""
        }
        ${
          s.error
            ? s.error.kind === "not_found"
              ? `<div style="margin:12px 26px;">${notWiredBanner("GET /search")}</div>`
              : `<div class="banner banner-error" style="margin:12px 26px;"><span>&#10005;</span><div style="flex:1;">${escapeHtml(fmtErr(s.error))}</div><button id="searchRetry" class="btn-ghost" style="color:var(--red);text-decoration:underline;">Retry</button></div>`
            : ""
        }

        <div class="split-body${s.detailOpen ? "" : " detail-collapsed"}" style="position:relative;">
          <div class="split-list-pane" data-role="results-list">
            ${
              s.loading
                ? `<div style="padding:18px;">${skeletonRows(10, 26)}</div>`
                : !s.ran
                ? `<div class="state-block"><div class="state-title">Search the archive</div><p class="state-body">Every article ever collected, not just today's. Try one of the suggestions below the box.</p>
                     <div class="row wrap" style="gap:8px;justify-content:center;">
                       ${SUGGESTIONS.map((x) => `<button class="chip-toggle" data-suggest="${escapeHtml(x)}" style="border-radius:999px;">${escapeHtml(x)}</button>`).join("")}
                     </div></div>`
                : dateRangeInvalid()
                ? ""
                : !s.results.length
                ? `<div class="state-block"><div class="state-title">No matches</div><p class="state-body">Every word has to appear in the same article. Try fewer words, or loosen the filters above.</p></div>`
                : s.results.map(resultRow).join("") +
                  (s.results.length < s.total
                    ? `<div data-role="load-sentinel" style="padding:14px 18px;text-align:center;">
                         <button id="loadMoreBtn" class="btn-ghost" style="font-size:12px;color:var(--tx3);" ${s.loadingMore ? "disabled" : ""}>${
                           s.loadingMore ? "Loading more…" : "Load more"
                         }</button>
                       </div>`
                    : `<div class="end-of-queue">end of results</div>`)
            }
          </div>
          ${
            s.detailOpen
              ? `<div class="split-detail-pane">
                   <div style="display:flex;justify-content:flex-end;padding:8px 12px 0;">
                     <button id="collapseDetailBtn" class="btn-ghost" aria-label="Collapse the reading pane"
                             aria-expanded="true" style="font-size:12px;">&#10005; Hide</button>
                   </div>
                   ${renderDetail()}
                 </div>`
              : `<button id="expandDetailBtn" class="detail-reopen" aria-label="Show the reading pane"
                         aria-expanded="false">&#8592; Reading pane</button>`
          }
        </div>
      </div>
    `;

    const again = root.querySelector('[data-role="results-list"]');
    if (again && keepScroll) again.scrollTop = keepScroll;

    bind();
  }

  /**
   * Load the next page as the reader nears the end.
   *
   * Same treatment as the Review queue, for the same reason: stopping
   * to aim at a button every twenty results interrupts scanning, which
   * is the entire activity here. The button stays as the keyboard path
   * and the retry after a failed page.
   *
   * Declared at mount scope, NOT beside this function: render() runs at
   * mount, render() calls bind(), and bind() arms this. A `let` further
   * down the module body would still be in its temporal dead zone and
   * would throw on every load. That exact mistake cost a debugging pass
   * on the Review queue.
   */
  function armInfiniteScroll() {
    if (listObserver) {
      listObserver.disconnect();
      listObserver = null;
    }
    if (typeof IntersectionObserver === "undefined") return; // button still works
    const sentinel = root.querySelector('[data-role="load-sentinel"]');
    const scroller = root.querySelector('[data-role="results-list"]');
    if (!sentinel || !scroller) return;
    listObserver = new IntersectionObserver(
      (entries) => {
        if (!alive) return;
        if (!entries.some((e) => e.isIntersecting)) return;
        if (s.loading || s.loadingMore) return;
        if (s.results.length >= s.total) return;
        runSearch({ page: s.page + 1, append: true });
      },
      { root: scroller, rootMargin: "600px 0px" }
    );
    listObserver.observe(sentinel);
  }

  // ── binding ──────────────────────────────────────────────────────────

  function bind() {
    const input = root.querySelector("#searchInput");
    if (input) {
      // No render on input: rebuilding the DOM per keystroke is what
      // tore the focused field out from under the caret elsewhere.
      input.addEventListener("input", (e) => (s.q = e.target.value));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") runSearch({ page: 1 });
      });
    }
    const searchBtn = root.querySelector("#searchBtn");
    if (searchBtn) searchBtn.addEventListener("click", () => runSearch({ page: 1 }));

    const clearQuery = root.querySelector("#clearQueryBtn");
    if (clearQuery)
      clearQuery.addEventListener("click", () => {
        s.q = "";
        s.ran = false;
        s.results = [];
        s.total = 0;
        writeUrl();
        render();
        const again = root.querySelector("#searchInput");
        if (again) again.focus();
      });

    const retry = root.querySelector("#searchRetry");
    if (retry) retry.addEventListener("click", () => runSearch({ page: s.page }));

    root.querySelectorAll("[data-suggest]").forEach((b) =>
      b.addEventListener("click", () => {
        s.q = b.getAttribute("data-suggest");
        runSearch({ page: 1 });
      })
    );

    root.querySelectorAll("[data-toggle-body]").forEach((b) =>
      b.addEventListener("click", () => {
        const id = b.getAttribute("data-toggle-body");
        const r = s.results.find((x) => String(x.id) === id);
        if (!r || !r.body) return;
        if (s.openBodies.has(r.id)) s.openBodies.delete(r.id);
        else s.openBodies.add(r.id);
        render();
      })
    );

    const dateFrom = root.querySelector('[data-role="date-from"]');
    const dateTo = root.querySelector('[data-role="date-to"]');
    if (dateFrom) dateFrom.addEventListener("change", () => { s.dateFrom = dateFrom.value; refilter(); });
    if (dateTo) dateTo.addEventListener("change", () => { s.dateTo = dateTo.value; refilter(); });

    root.querySelectorAll("[data-lang]").forEach((b) =>
      b.addEventListener("click", () => { s.lang = b.getAttribute("data-lang"); refilter(); })
    );
    root.querySelectorAll("[data-status]").forEach((b) =>
      b.addEventListener("click", () => { s.status = b.getAttribute("data-status"); refilter(); })
    );
    root.querySelectorAll("[data-clear-filter]").forEach((b) =>
      b.addEventListener("click", () => clearFilter(b.getAttribute("data-clear-filter")))
    );

    const srcMenuBtn = root.querySelector("#srcMenuBtn");
    if (srcMenuBtn) srcMenuBtn.addEventListener("click", () => { s.srcMenuOpen = !s.srcMenuOpen; render(); });
    root.querySelectorAll("[data-src]").forEach((cb) =>
      cb.addEventListener("change", () => {
        const name = cb.getAttribute("data-src");
        if (cb.checked) s.srcOff = s.srcOff.filter((x) => x !== name);
        else if (!s.srcOff.includes(name)) s.srcOff.push(name);
        // Keep the menu open: hiding several sources one at a time is
        // the normal case, and closing it after each tick made that
        // four clicks instead of one.
        refilter();
      })
    );
    const srcAll = root.querySelector("#srcAllBtn");
    if (srcAll) srcAll.addEventListener("click", () => { s.srcOff = []; refilter(); });
    const srcNone = root.querySelector("#srcNoneBtn");
    if (srcNone) srcNone.addEventListener("click", () => { s.srcOff = s.sources.map((x) => x.name); refilter(); });

    const clearFiltersBtn = root.querySelector("#clearFiltersBtn");
    if (clearFiltersBtn) clearFiltersBtn.addEventListener("click", resetFilters);

    // Selecting a result. Rows are role="button" with tabindex, so the
    // keyboard reaches them; Enter and Space activate, as a real button
    // would.
    const select = (id) => {
      s.selId = id;
      s.detailOpen = true;
      render();
    };
    root.querySelectorAll("[data-result]").forEach((row) => {
      const id = row.getAttribute("data-result");
      const pick = () => {
        const hit = s.results.find((x) => String(x.id) === id);
        if (hit) select(hit.id);
      };
      row.addEventListener("click", pick);
      row.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          pick();
        }
      });
    });

    const collapseDetail = root.querySelector("#collapseDetailBtn");
    if (collapseDetail)
      collapseDetail.addEventListener("click", () => {
        s.detailOpen = false;
        render();
        root.querySelector("#expandDetailBtn")?.focus();
      });
    const expandDetail = root.querySelector("#expandDetailBtn");
    if (expandDetail)
      expandDetail.addEventListener("click", () => {
        s.detailOpen = true;
        render();
        root.querySelector("#collapseDetailBtn")?.focus();
      });

    const loadMoreBtn = root.querySelector("#loadMoreBtn");
    if (loadMoreBtn) loadMoreBtn.addEventListener("click", () => runSearch({ page: s.page + 1, append: true }));
    armInfiniteScroll();
  }

  return {
    destroy() {
      alive = false;
      if (listObserver) {
        listObserver.disconnect();
        listObserver = null;
      }
      // Leave the address bar as this screen found it. Without this the
      // shell's pushRoute carries `?q=...` onto whatever screen comes
      // next, since it preserves window.location.search by design.
      if (typeof window !== "undefined" && window.history) {
        const p = new URLSearchParams(window.location.search);
        for (const k of URL_KEYS) p.delete(k);
        const qs = p.toString();
        window.history.replaceState(
          window.history.state,
          "",
          window.location.pathname + (qs ? "?" + qs : "") + window.location.hash
        );
      }
    },
  };
}
