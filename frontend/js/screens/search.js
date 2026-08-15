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
    page: 1,
    dateFrom: "",
    dateTo: "",
    lang: "",
    status: "",
    sources: [],
    srcOff: [],
    srcMenuOpen: false,
  };

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

  async function runSearch({ page = 1, push = true } = {}) {
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
    s.loading = true;
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
      s.results = (data && data.results) || [];
      s.total = (data && data.total) ?? s.results.length;
      s.corpusSize = data && data.corpusSize;
      // The source list comes back with the results, so it only exists
      // once a search has run. Keep the previous list when a response
      // omits it, or opening the picker after a zero-result search
      // would show nothing to un-hide.
      if (data && data.sources && data.sources.length) s.sources = data.sources;
      s.loading = false;
      s.openBodies.clear();
      render();
    } catch (err) {
      if (!alive) return;
      s.loading = false;
      s.error = err;
      render();
    }
  }

  function goToPage(n) {
    const target = Math.min(totalPages(), Math.max(1, n));
    if (target === s.page) return;
    runSearch({ page: target });
    const scroller = document.querySelector(".main-area");
    if (scroller) scroller.scrollTop = 0;
  }

  // ── fragments ────────────────────────────────────────────────────────

  function resultCard(r) {
    const t = terms();
    const open = s.openBodies.has(r.id);
    const statusLabel = r.status === "approved" ? "Approved" : r.status === "rejected" ? "Rejected" : "";
    const statusBg = r.status === "approved" ? "var(--acsoft)" : "var(--redsoft)";
    const statusFg = r.status === "approved" ? "var(--actx)" : "var(--red)";

    // Where did it match? A body-only hit is the case the old screen
    // hid completely, and the one most worth showing.
    const inTitle = t.some((w) => (r.title || "").toLowerCase().includes(w));
    const inTitleEn = t.some((w) => (r.titleEn || "").toLowerCase().includes(w));
    const inBody = t.some((w) => (r.body || "").toLowerCase().includes(w));
    const bodyOnly = !inTitle && !inTitleEn && inBody;

    return `<div style="border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:15px 18px;">
      <div class="row wrap" style="gap:10px;margin-bottom:7px;align-items:center;">
        <span class="tag">${escapeHtml(r.source)}</span>
        <span class="mono" style="font-size:10.5px;color:var(--tx3);">${escapeHtml(r.day)}</span>
        ${statusLabel ? `<span class="pill" style="background:${statusBg};color:${statusFg};text-transform:uppercase;letter-spacing:.6px;font-size:9px;">${statusLabel}</span>` : ""}
        ${bodyOnly ? `<span class="mono" style="font-size:9.5px;letter-spacing:.6px;text-transform:uppercase;color:var(--tx4);">matched in body</span>` : ""}
      </div>
      <a href="${escapeHtml(r.url || "#")}" target="_blank" rel="noopener noreferrer"
         style="display:block;font-family:'Lora',Georgia,serif;font-size:15.5px;font-weight:500;color:var(--tx);line-height:1.4;">${highlightTerms(
           r.title,
           t
         )}</a>
      ${
        r.titleEn && r.titleEn !== r.title
          ? `<div style="font-size:13px;color:var(--tx2);font-style:italic;margin-top:4px;">${highlightTerms(r.titleEn, t)}</div>`
          : ""
      }
      ${
        r.body
          ? `<div style="margin-top:9px;font-size:12.5px;line-height:1.65;color:var(--tx3);">${highlightTerms(
              snippetAround(r.body, t),
              t
            )}</div>`
          : ""
      }
      <div class="row" style="gap:14px;margin-top:10px;align-items:center;">
        <button data-toggle-body="${r.id}" class="btn-ghost" style="color:${
          r.body ? "var(--actx)" : "var(--tx3)"
        };font-size:12.5px;font-weight:600;cursor:${r.body ? "pointer" : "default"};padding:0;">${
          r.body ? (open ? "Hide full article" : "Read full article") : "No article text"
        }</button>
      </div>
      ${
        open && r.body
          ? `<div style="margin-top:11px;padding:14px 16px;background:var(--bg);border:1px solid var(--line);border-radius:9px;font-family:'Lora',Georgia,serif;font-size:14px;line-height:1.75;color:var(--tx2);white-space:pre-wrap;">${highlightTerms(
              r.body,
              t
            )}</div>`
          : ""
      }
    </div>`;
  }

  function pagerHtml() {
    const pages = totalPages();
    if (pages <= 1) return "";
    return `<div class="row" style="justify-content:center;gap:10px;margin-top:22px;align-items:center;">
      <button data-page="1" class="btn btn-outline" ${s.page <= 1 ? "disabled" : ""}>First</button>
      <button data-page="${s.page - 1}" class="btn btn-outline" ${s.page <= 1 ? "disabled" : ""}>&larr; Prev</button>
      <span class="mono" style="font-size:11px;color:var(--tx3);min-width:110px;text-align:center;">Page ${s.page} of ${pages}</span>
      <button data-page="${s.page + 1}" class="btn btn-outline" ${s.page >= pages ? "disabled" : ""}>Next &rarr;</button>
      <button data-page="${pages}" class="btn btn-outline" ${s.page >= pages ? "disabled" : ""}>Last</button>
    </div>`;
  }

  function resultsHeader() {
    if (!s.ran || s.loading || s.error) return "";
    const first = (s.page - 1) * PAGE_SIZE + 1;
    const last = Math.min(s.total, first + s.results.length - 1);
    return `<div class="row-between wrap" style="gap:10px;margin-bottom:14px;">
      <div class="mono" style="font-size:11px;color:var(--tx3);">
        ${
          s.total
            ? `${first.toLocaleString()} to ${last.toLocaleString()} of ${s.total.toLocaleString()} matches`
            : "no matches"
        }${s.corpusSize ? ` · searched ${s.corpusSize.toLocaleString()} articles` : ""}
      </div>
      ${
        s.total
          ? `<div class="mono" style="font-size:11px;color:var(--tx4);">every word must appear · newest first</div>`
          : ""
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
    root.innerHTML = `
      <div class="page page-narrow">
        <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0 0 18px;max-width:62ch;">
          Every article ever collected, searched across the headline, the English translation and the
          body text, on all dates and in any review state. Every word you type must appear.
          Matches are highlighted so you can see why a result came back.
        </p>
        <div class="row" style="gap:10px;margin-bottom:8px;">
          <div class="search-box" style="height:46px;padding:0 14px;max-width:none;">
            <span style="color:var(--tx4);">&#8981;</span>
            <input id="searchInput" value="${escapeHtml(s.q)}" placeholder="e.g. hormuz oil tariff" style="font-size:14.5px;" />
            ${s.q ? `<button id="clearQueryBtn" class="btn-ghost" style="color:var(--tx4);padding:0 4px;font-size:14px;" title="Clear">&#10005;</button>` : ""}
          </div>
          <button id="searchBtn" class="btn btn-primary" style="padding:0 22px;height:46px;font-size:14px;">Search</button>
        </div>
        <div class="row wrap" style="gap:8px;margin-bottom:16px;">
          <span style="font-size:12px;color:var(--tx4);padding:5px 0;">Try:</span>
          ${SUGGESTIONS.map((x) => `<button class="chip-toggle" data-suggest="${escapeHtml(x)}" style="border-radius:999px;">${escapeHtml(x)}</button>`).join("")}
        </div>

        ${filterBar()}

        ${
          dateRangeInvalid()
            ? `<div class="banner banner-warn"><span>&#9888;</span><div style="flex:1;">The start date is after the end date, so nothing can match. Swap them or clear one.</div></div>`
            : ""
        }

        ${resultsHeader()}

        ${
          s.loading
            ? `<div class="stack" style="gap:10px;">${Array.from({ length: 4 })
                .map(() => `<div class="card" style="padding:15px 18px;">${skeletonRows(3)}</div>`)
                .join("")}</div>`
            : s.error
            ? s.error.kind === "not_found"
              ? notWiredBanner("GET /search")
              : `<div class="banner banner-error"><span>&#10005;</span><div style="flex:1;">${escapeHtml(fmtErr(s.error))}</div><button id="searchRetry" class="btn-ghost" style="color:var(--red);text-decoration:underline;">Retry</button></div>`
            : !s.ran
            ? `<div class="state-block"><div class="state-title">Search the archive</div><p class="state-body">Type a word or phrase above. The archive covers every article ever collected, not just today's.</p></div>`
            : dateRangeInvalid()
            ? ""
            : !s.results.length
            ? `<div class="state-block"><div class="state-title">No matches</div><p class="state-body">Every word has to appear in the same article. Try fewer words, or loosen the date, language, source and status filters.</p></div>`
            : `<div class="stack" style="gap:10px;">${s.results.map(resultCard).join("")}</div>`
        }

        ${s.ran && !s.loading && !s.error && s.results.length ? pagerHtml() : ""}
      </div>
    `;
    bind();
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

    root.querySelectorAll("[data-page]").forEach((b) =>
      b.addEventListener("click", () => goToPage(Number(b.getAttribute("data-page"))))
    );
  }

  return {
    destroy() {
      alive = false;
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
