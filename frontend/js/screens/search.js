// Search archive (frontend-contract.md §3.6).
//
// API (app/api/routers/search.py):
//   GET /search?q=&page=&date_from=&date_to=&lang=&status=&exclude_source= ->
//     { results: [{ id, source, day, title, titleEn, url, status, body }],
//       total, corpusSize, sources: [{name, count}] }
//
// SEARCH-FILTERS: filters (date range, source, language, review status)
// are combinable and mirror review.js's filter shapes/interaction
// pattern (exclude-list source picker, date-range inputs, lang seg
// group) for consistency across the two screens — see
// app/services/search.py's module docstring for the `status` vocabulary
// ("matched" / "approved" / "rejected" / "never_matched" / all), which
// covers the owner's ask that search also surface articles that were
// never keyword-matched (and thus never AI-scored).
//
// Suggested-query chips are static example text per §8 item 11 (the
// mock's copy — "Try:" — doesn't disambiguate whether these should be
// server-supplied trending queries; kept as static client copy here,
// the lower-risk reading, since no endpoint proposes them).

import { api } from "../api.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows } from "../util.js";

const SUGGESTIONS = ["hormuz oil", "spectrum auction", "ভ্যাট", "submarine cable"];
const STATUSES = [
  ["", "All"],
  ["matched", "Matched"],
  ["approved", "Approved"],
  ["rejected", "Rejected"],
  ["never_matched", "Never matched"],
];

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

  render();

  function hasActiveFilters() {
    return !!(s.dateFrom || s.dateTo || s.lang || s.status || s.srcOff.length);
  }

  function resetFilters() {
    s.dateFrom = "";
    s.dateTo = "";
    s.lang = "";
    s.status = "";
    s.srcOff = [];
    if (s.ran) runSearch();
    else render();
  }

  async function runSearch({ page = 1 } = {}) {
    if (!s.q.trim()) return;
    s.ran = true;
    s.page = page;
    s.loading = true;
    s.error = null;
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
      s.sources = (data && data.sources) || [];
      s.loading = false;
      render();
    } catch (err) {
      if (!alive) return;
      s.loading = false;
      s.error = err;
      render();
    }
  }

  function render() {
    root.innerHTML = `
      <div class="page page-narrow">
        <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0 0 18px;max-width:62ch;">Searches every article ever collected — headline, English translation and body text — across all dates and statuses. All words must appear. Newest first.</p>
        <div class="row" style="gap:10px;margin-bottom:8px;">
          <div class="search-box" style="height:46px;padding:0 14px;">
            <span style="color:var(--tx4);">&#8981;</span>
            <input id="searchInput" value="${escapeHtml(s.q)}" placeholder="e.g. hormuz oil tariff" style="font-size:14.5px;" />
          </div>
          <button id="searchBtn" class="btn btn-primary" style="padding:0 22px;height:46px;font-size:14px;">Search</button>
        </div>
        <div class="row wrap" style="gap:8px;margin-bottom:16px;">
          <span style="font-size:12px;color:var(--tx4);padding:5px 0;">Try:</span>
          ${SUGGESTIONS.map((x) => `<button class="chip-toggle" data-suggest="${escapeHtml(x)}" style="border-radius:999px;">${escapeHtml(x)}</button>`).join("")}
        </div>

        <div class="row wrap" style="gap:10px;margin-bottom:16px;align-items:center;">
          <div class="date-range-group">
            <span class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx4);">Date</span>
            <input type="date" data-role="date-from" class="date-input" value="${escapeHtml(s.dateFrom)}" />
            <span style="color:var(--tx4);">–</span>
            <input type="date" data-role="date-to" class="date-input" value="${escapeHtml(s.dateTo)}" />
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
          <div class="seg-group">
            ${STATUSES.map(([k, l]) => `<button class="seg-btn ${s.status === k ? "active" : ""}" data-status="${k}">${l}</button>`).join("")}
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
                  .join("") || `<div style="font-size:12px;color:var(--tx3);padding:8px;">Run a search to see sources.</div>`}
              </div>
            </div>`
                : ""
            }
          </div>
          ${hasActiveFilters() ? `<button id="clearFiltersBtn" class="btn-ghost" style="font-size:12px;">Reset filters</button>` : ""}
        </div>

        ${
          s.ran && !s.loading && !s.error
            ? `<div class="mono" style="font-size:11px;color:var(--tx3);margin-bottom:14px;">Showing ${s.results.length} of ${s.total}${
                s.corpusSize ? " · searched " + s.corpusSize.toLocaleString() + " articles across all dates" : ""
              }</div>`
            : ""
        }

        ${
          s.loading
            ? `<div class="stack">${Array.from({ length: 4 })
                .map(() => `<div class="card" style="padding:15px 18px;">${skeletonRows(3)}</div>`)
                .join("")}</div>`
            : s.error
            ? s.error.kind === "not_found"
              ? notWiredBanner("GET /search")
              : `<div class="banner banner-error"><span>&#10005;</span><div>${escapeHtml(fmtErr(s.error))}</div></div>`
            : s.ran && !s.results.length
            ? `<div class="state-block"><div class="state-title">No matches</div><p class="state-body">Loosen the date, language, source or status filters, or try fewer/different words.</p></div>`
            : `<div class="stack" style="gap:10px;">${s.results.map((r) => resultCard(r)).join("")}</div>`
        }

        ${s.ran && !s.loading && !s.error && s.total > s.results.length ? pagerHtml() : ""}
      </div>
    `;
    bind();
  }

  function pagerHtml() {
    const pageSize = s.results.length || 20;
    const totalPages = Math.max(1, Math.ceil(s.total / pageSize) || 1);
    return `<div class="row" style="justify-content:center;gap:14px;margin-top:20px;">
      <button id="prevPageBtn" class="btn btn-outline" ${s.page <= 1 ? "disabled" : ""}>&larr; Prev</button>
      <span class="mono" style="font-size:11px;color:var(--tx3);">Page ${s.page}</span>
      <button id="nextPageBtn" class="btn btn-outline" ${s.page >= totalPages ? "disabled" : ""}>Next &rarr;</button>
    </div>`;
  }

  function resultCard(r) {
    const statusLabel = r.status === "approved" ? "Approved" : r.status === "rejected" ? "Rejected" : "";
    const statusBg = r.status === "approved" ? "var(--acsoft)" : "var(--redsoft)";
    const statusFg = r.status === "approved" ? "var(--actx)" : "var(--red)";
    const open = s.openBodies.has(r.id);
    return `<div style="border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:15px 18px;">
      <div class="row" style="gap:10px;margin-bottom:7px;">
        <span class="tag">${escapeHtml(r.source)}</span>
        <span class="mono" style="font-size:10.5px;color:var(--tx3);">${escapeHtml(r.day)}</span>
        ${statusLabel ? `<span class="pill" style="background:${statusBg};color:${statusFg};text-transform:uppercase;letter-spacing:.6px;font-size:9px;">${statusLabel}</span>` : ""}
      </div>
      <a href="${escapeHtml(r.url || "#")}" target="_blank" style="display:block;font-family:'Lora',Georgia,serif;font-size:15.5px;font-weight:500;color:var(--tx);line-height:1.4;">${escapeHtml(r.title)}</a>
      ${r.titleEn ? `<div style="font-size:13px;color:var(--tx2);font-style:italic;margin-top:4px;">${escapeHtml(r.titleEn)}</div>` : ""}
      <button data-toggle-body="${r.id}" class="btn-ghost" style="display:inline-flex;align-items:center;gap:7px;margin-top:10px;color:${
      r.body ? "var(--actx)" : "var(--tx3)"
    };font-size:12.5px;font-weight:600;cursor:${r.body ? "pointer" : "default"};padding:0;">${
      r.body ? (open ? "⌄ Hide article" : "› Read article") : "— No article text"
    }</button>
      ${open && r.body ? `<div style="margin-top:11px;padding:14px 16px;background:var(--bg);border:1px solid var(--line);border-radius:9px;font-family:'Lora',Georgia,serif;font-size:14px;line-height:1.75;color:var(--tx2);white-space:pre-wrap;">${escapeHtml(
        r.body
      )}</div>` : ""}
    </div>`;
  }

  function bind() {
    const input = root.querySelector("#searchInput");
    if (input) {
      input.addEventListener("input", (e) => (s.q = e.target.value));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") runSearch();
      });
    }
    const btn = root.querySelector("#searchBtn");
    if (btn) btn.addEventListener("click", () => runSearch());
    root.querySelectorAll("[data-suggest]").forEach((b) =>
      b.addEventListener("click", () => {
        s.q = b.getAttribute("data-suggest");
        runSearch();
      })
    );
    root.querySelectorAll("[data-toggle-body]").forEach((b) =>
      b.addEventListener("click", () => {
        const id = Number(b.getAttribute("data-toggle-body"));
        const r = s.results.find((x) => x.id === id);
        if (!r || !r.body) return;
        if (s.openBodies.has(id)) s.openBodies.delete(id);
        else s.openBodies.add(id);
        render();
      })
    );

    const dateFrom = root.querySelector('[data-role="date-from"]');
    const dateTo = root.querySelector('[data-role="date-to"]');
    if (dateFrom) dateFrom.addEventListener("change", () => { s.dateFrom = dateFrom.value; if (s.ran) runSearch(); });
    if (dateTo) dateTo.addEventListener("change", () => { s.dateTo = dateTo.value; if (s.ran) runSearch(); });

    root.querySelectorAll("[data-lang]").forEach((b) =>
      b.addEventListener("click", () => {
        s.lang = b.getAttribute("data-lang");
        if (s.ran) runSearch();
        else render();
      })
    );
    root.querySelectorAll("[data-status]").forEach((b) =>
      b.addEventListener("click", () => {
        s.status = b.getAttribute("data-status");
        if (s.ran) runSearch();
        else render();
      })
    );

    const srcMenuBtn = root.querySelector("#srcMenuBtn");
    if (srcMenuBtn) srcMenuBtn.addEventListener("click", () => { s.srcMenuOpen = !s.srcMenuOpen; render(); });
    root.querySelectorAll("[data-src]").forEach((cb) =>
      cb.addEventListener("change", () => {
        const name = cb.getAttribute("data-src");
        if (cb.checked) s.srcOff = s.srcOff.filter((x) => x !== name);
        else s.srcOff.push(name);
        if (s.ran) runSearch();
      })
    );
    const srcAll = root.querySelector("#srcAllBtn");
    if (srcAll) srcAll.addEventListener("click", () => { s.srcOff = []; if (s.ran) runSearch(); });
    const srcNone = root.querySelector("#srcNoneBtn");
    if (srcNone) srcNone.addEventListener("click", () => { s.srcOff = s.sources.map((x) => x.name); if (s.ran) runSearch(); });

    const clearFiltersBtn = root.querySelector("#clearFiltersBtn");
    if (clearFiltersBtn) clearFiltersBtn.addEventListener("click", resetFilters);

    const prevBtn = root.querySelector("#prevPageBtn");
    if (prevBtn) prevBtn.addEventListener("click", () => runSearch({ page: s.page - 1 }));
    const nextBtn = root.querySelector("#nextPageBtn");
    if (nextBtn) nextBtn.addEventListener("click", () => runSearch({ page: s.page + 1 }));
  }

  return {
    destroy() {
      alive = false;
    },
  };
}
