// Search archive (frontend-contract.md §3.6).
//
// Expected API (proposal — app/api/routers/search.py is a stub):
//   GET /search?q=&page= ->
//     { results: [{ id, source, day, title, titleEn, url, status, body }],
//       total, corpusSize }
//
// Suggested-query chips are static example text per §8 item 11 (the
// mock's copy — "Try:" — doesn't disambiguate whether these should be
// server-supplied trending queries; kept as static client copy here,
// the lower-risk reading, since no endpoint proposes them).

import { api } from "../api.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows } from "../util.js";

const SUGGESTIONS = ["hormuz oil", "spectrum auction", "ভ্যাট", "submarine cable"];

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
  };

  render();

  async function runSearch() {
    if (!s.q.trim()) return;
    s.ran = true;
    s.loading = true;
    s.error = null;
    render();
    try {
      const data = await api.get("/search", { query: { q: s.q } });
      if (!alive) return;
      s.results = (data && data.results) || [];
      s.total = (data && data.total) ?? s.results.length;
      s.corpusSize = data && data.corpusSize;
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
        <div class="row wrap" style="gap:8px;margin-bottom:20px;">
          <span style="font-size:12px;color:var(--tx4);padding:5px 0;">Try:</span>
          ${SUGGESTIONS.map((x) => `<button class="chip-toggle" data-suggest="${escapeHtml(x)}" style="border-radius:999px;">${escapeHtml(x)}</button>`).join("")}
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
            ? `<div class="state-block"><div class="state-title">No matches</div><p class="state-body">Try fewer or different words.</p></div>`
            : `<div class="stack" style="gap:10px;">${s.results.map((r) => resultCard(r)).join("")}</div>`
        }
      </div>
    `;
    bind();
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
    if (btn) btn.addEventListener("click", runSearch);
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
  }

  return {
    destroy() {
      alive = false;
    },
  };
}
