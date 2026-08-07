// Review queue — the core triage tool (frontend-contract.md §3.3).
//
// Expected API (proposal — app/api/routers/review.py is a stub, zero
// routes registered; GET/PATCH/POST below all currently 404):
//
//   GET /articles?status=&tier=&lang=&source=&q=&sort=&starred= ->
//     { items: [{ id, title, source, time, section, score, dupCount,
//                 isStarred, status, titleEn, url, reason,
//                 keywords: [], body, bodyFetched: bool, note,
//                 dups: [{id,source,title,url}]|null,
//                 topicPeers: [{id,source,title,score}]|null, topic }],
//       total, sources: [{name, count}] }
//   PATCH /articles/{id}            { status?, starred?, note? }
//   POST  /articles/bulk            { ids: [], action: "approve"|"reject"|"star" }
//   POST  /articles/{id}/dups/{dupId}/keep
//   POST  /articles/{id}/dups/{dupId}/drop
//
// Tier banding (score -> High/Medium/Low/Least) and dup/topic-peer
// colors are computed client-side from the raw score, per §3.3's
// Server/Client split — the server sends the score, not the label.

import { api, ApiError } from "../api.js";
import { escapeHtml, tier, debounce, fmtErr, skeletonRows, notWiredBanner } from "../util.js";

const TIER_KEYS = ["red", "yellow", "green", "magenta"];
const TIER_META = {
  red: { title: "High (8–10)", dot: "var(--red)" },
  yellow: { title: "Medium (6–7)", dot: "var(--amber)" },
  green: { title: "Low (5)", dot: "var(--green)" },
  magenta: { title: "Least (<5)", dot: "var(--violet)" },
};

export function mountReview(root, { navigate }) {
  let alive = true;
  const s = {
    q: "",
    sort: "relevance",
    tiers: { red: true, yellow: true, green: true, magenta: true },
    lang: "",
    srcOff: [],
    showDecided: false,
    starredOnly: false,
    srcMenuOpen: false,
    checked: new Set(),
    selId: null,
    dupOpen: false,
    topicOpen: false,
    items: [],
    sources: [],
    loading: true,
    error: null,
    noteDrafts: {},
  };

  const debouncedFetch = debounce(fetchList, 300);

  const onKeyDown = (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) {
      if (e.key === "Escape") t.blur();
      return;
    }
    const list = s.items;
    const i = list.findIndex((a) => a.id === s.selId);
    if (e.key === "j" || e.key === "J" || e.key === "ArrowDown") {
      e.preventDefault();
      const n = list[Math.min(list.length - 1, i + 1)];
      if (n) selectArticle(n.id);
    } else if (e.key === "k" || e.key === "K" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = list[Math.max(0, i - 1)];
      if (n) selectArticle(n.id);
    } else if (e.key === "a" || e.key === "A") {
      e.preventDefault();
      if (s.selId) decide(s.selId, "approved");
    } else if (e.key === "r" || e.key === "R") {
      e.preventDefault();
      if (s.selId) decide(s.selId, "rejected");
    } else if (e.key === "x" || e.key === "X") {
      e.preventDefault();
      if (s.selId) toggleCheck(s.selId);
    } else if (e.key === "s" || e.key === "S") {
      e.preventDefault();
      if (s.selId) toggleStar(s.selId);
    } else if (e.key === "/") {
      e.preventDefault();
      const el = root.querySelector('[data-role="queue-filter"]');
      if (el) el.focus();
    }
  };
  document.addEventListener("keydown", onKeyDown);

  render();
  fetchList();

  async function fetchList() {
    s.loading = true;
    s.error = null;
    render();
    try {
      const activeTiers = TIER_KEYS.filter((k) => s.tiers[k]);
      const data = await api.get("/articles", {
        query: {
          q: s.q || undefined,
          sort: s.sort,
          tier: activeTiers.length < 4 ? activeTiers.join(",") : undefined,
          lang: s.lang || undefined,
          exclude_source: s.srcOff.join(",") || undefined,
          status: s.showDecided ? undefined : "pending",
          starred: s.starredOnly ? "true" : undefined,
        },
      });
      if (!alive) return;
      s.items = (data && data.items) || [];
      s.sources = (data && data.sources) || [];
      s.loading = false;
      if (s.selId && !s.items.some((a) => a.id === s.selId)) s.selId = null;
      if (!s.selId && s.items.length) s.selId = s.items[0].id;
      render();
    } catch (err) {
      if (!alive) return;
      s.loading = false;
      s.error = err;
      render();
    }
  }

  function selectArticle(id) {
    s.selId = id;
    s.dupOpen = false;
    s.topicOpen = false;
    render();
  }

  function toggleCheck(id) {
    if (s.checked.has(id)) s.checked.delete(id);
    else s.checked.add(id);
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
    if (!s.showDecided) {
      list.splice(i, 1);
      const next = list[i] || list[i - 1];
      s.selId = next ? next.id : null;
    }
    s.dupOpen = false;
    s.topicOpen = false;
    render();
    api.patch(`/articles/${id}`, { status }).catch((err) => {
      s.error = err;
      fetchList();
    });
  }

  async function bulk(action) {
    const ids = Array.from(s.checked);
    if (!ids.length) return;
    s.checked.clear();
    render();
    try {
      await api.post("/articles/bulk", { ids, action });
    } catch (err) {
      /* surfaced on next list refresh */
    }
    fetchList();
  }

  function resetFilters() {
    s.q = "";
    s.tiers = { red: true, yellow: true, green: true, magenta: true };
    s.lang = "";
    s.srcOff = [];
    s.starredOnly = false;
    fetchList();
  }

  function render() {
    const cur = s.items.find((a) => a.id === s.selId) || null;
    root.innerHTML = `
      <div class="review-shell">
        <div class="review-toolbar">
          <div class="review-toolbar-row">
            <div class="search-box">
              <span style="color:var(--tx4);font-size:12px;">&#8981;</span>
              <input data-role="queue-filter" placeholder="Filter the queue…  /" value="${escapeHtml(s.q)}" />
            </div>
            <div class="seg-group">
              ${[
                ["relevance", "Relevance"],
                ["date", "Newest"],
                ["source", "Source"],
              ]
                .map(([k, l]) => `<button class="seg-btn ${s.sort === k ? "active" : ""}" data-sort="${k}">${l}</button>`)
                .join("")}
            </div>
            <div class="tier-group">
              <span class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx4);margin-right:2px;">Rel</span>
              ${TIER_KEYS.map((k) => {
                const meta = TIER_META[k];
                const on = s.tiers[k];
                return `<button class="tier-dot-btn ${on ? "active" : ""}" data-tier="${k}" title="${meta.title}">
                  <span class="tier-dot" style="background:${meta.dot};opacity:${on ? 1 : 0.22}"></span>
                </button>`;
              }).join("")}
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
            <button id="decidedBtn" class="chk-btn" style="${s.showDecided ? "color:var(--actx);" : ""}">${s.showDecided ? "Hide decided" : "Show decided"}</button>
            <button id="starredBtn" class="chk-btn" style="${s.starredOnly ? "border-color:var(--amber);color:var(--amber);" : ""}">&#9733; ${
      s.items.filter((a) => a.isStarred).length
    }</button>
            <div class="grow"></div>
            <button id="goExportBtn" class="btn btn-primary" style="height:36px;padding:0 15px;white-space:nowrap;">&#8595; Export approved</button>
          </div>
          ${
            s.checked.size
              ? `<div class="bulk-bar">
              <span style="font-size:13px;font-weight:600;color:var(--actx);">${s.checked.size} selected</span>
              <button id="bulkApprove" class="btn btn-primary" style="padding:7px 14px;">&#10003; Approve all</button>
              <button id="bulkReject" class="btn btn-danger-outline" style="padding:7px 14px;">&#10005; Reject all</button>
              <button id="bulkStar" class="btn-sm" style="padding:7px 14px;">&#9733; Star all</button>
              <div class="grow"></div>
              <button id="clearChecked" class="btn-ghost" style="cursor:pointer;font-size:12px;">Clear selection</button>
            </div>`
              : ""
          }
        </div>

        ${s.error ? errorRow(s.error) : ""}

        <div class="review-body">
          <div class="review-list-pane">
            <div class="review-list-head">
              <div class="mono" style="font-size:10px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);">${
                s.loading ? "Loading…" : `${s.items.length} shown`
              }</div>
              <div class="mono" style="font-size:10px;color:var(--tx4);">J / K to move</div>
            </div>
            ${renderList(cur)}
            ${s.items.length && !s.loading ? `<div class="end-of-queue">end of queue</div>` : ""}
          </div>
          <div class="review-detail-pane">
            ${renderDetail(cur)}
          </div>
        </div>

        <div class="hints-bar">
          ${[
            ["J K", "move"],
            ["A", "approve"],
            ["R", "reject"],
            ["X", "select"],
            ["S", "star"],
            ["/", "filter"],
          ]
            .map(([k, w]) => `<span style="display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--tx2);white-space:nowrap;"><b class="hint-key">${k}</b>${w}</span>`)
            .join("")}
          <button id="hideHints" class="btn-ghost" style="font-size:14px;line-height:1;padding:0 0 0 4px;">&#10005;</button>
        </div>
      </div>
    `;
    bind();
  }

  function errorRow(err) {
    if (err instanceof ApiError && err.kind === "not_found") {
      return `<div style="padding:12px 26px 0;">${notWiredBanner("GET /articles")}</div>`;
    }
    return `<div style="padding:12px 26px 0;"><div class="banner banner-error"><span>&#10005;</span><div style="flex:1">${escapeHtml(
      fmtErr(err)
    )}</div><button id="reviewRetry" class="btn-ghost" style="color:var(--red);text-decoration:underline;">Retry</button></div></div>`;
  }

  function renderList(cur) {
    if (s.loading) {
      return `<div style="padding:13px 18px;">${skeletonRows(8, 46)}</div>`;
    }
    if (s.error) return "";
    if (!s.items.length) {
      return `<div class="state-block">
        <div class="state-title">Nothing matches those filters</div>
        <p class="state-body">Loosen the relevance, language or source filters to see more of today's matched stories.</p>
        <button id="resetFiltersBtn" class="btn btn-outline">Reset filters</button>
      </div>`;
    }
    return s.items
      .map((a) => {
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
              <span class="pill" style="background:${t.bg};color:${t.color}"><span class="pill-dot" style="background:${t.color}"></span>${t.label}</span>
              ${a.dupCount ? `<span class="pill" style="background:var(--acsoft);color:var(--actx);">+${a.dupCount}</span>` : ""}
              ${a.isStarred ? `<span style="font-size:11px;color:var(--amber);">&#9733;</span>` : ""}
              ${statusLabel ? `<span class="pill" style="background:${statusBg};color:${statusFg};text-transform:uppercase;letter-spacing:.6px;font-size:9px;">${statusLabel}</span>` : ""}
            </div>
            <div class="article-title">${escapeHtml(a.title)}</div>
            <div class="article-sub">${escapeHtml(a.time || "")} · ${escapeHtml(a.section || "")}</div>
          </div>
        </div>`;
      })
      .join("");
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
    const dups = cur.dups || null;
    const peers = cur.topicPeers || null;
    const bodyMeta = cur.body ? `${cur.body.split(/\s+/).filter(Boolean).length} words · fetched` : "not fetched";
    const noteVal = s.noteDrafts[cur.id] !== undefined ? s.noteDrafts[cur.id] : cur.note || "";
    return `<div class="detail-wrap">
      <div class="detail-head">
        <span class="tag" style="font-size:12px;padding:4px 11px;">${escapeHtml(cur.source)}</span>
        <span class="pill" style="background:${t.bg};color:${t.color};font-size:11px;padding:4px 10px;"><span class="pill-dot" style="background:${t.color}"></span>${t.label} · ${cur.score}/10</span>
        <span class="mono" style="font-size:11px;color:var(--tx2);">${escapeHtml(cur.section || "")}</span>
        <div class="grow"></div>
        <button id="starCurBtn" class="icon-btn" title="Star for later" style="color:${cur.isStarred ? "var(--amber)" : "var(--tx4)"};border-color:${cur.isStarred ? "var(--amber)" : "var(--line)"}">&#9733;</button>
        <span class="mono" style="font-size:11px;color:var(--tx3);">published ${escapeHtml(cur.time || "")}</span>
      </div>
      <h2 class="detail-title">${escapeHtml(cur.title)}</h2>
      ${cur.titleEn ? `<div class="detail-title-en">${escapeHtml(cur.titleEn)}</div>` : ""}
      <a href="${escapeHtml(cur.url || "#")}" target="_blank" style="display:inline-flex;align-items:center;gap:6px;font-size:12.5px;margin-bottom:20px;">Open at source &#8599;</a>
      <div class="detail-actions">
        <button id="approveCurBtn" class="btn btn-primary" style="flex:1;justify-content:center;gap:9px;padding:13px 20px;font-size:14px;">&#10003; Approve <span class="mono" style="font-size:11px;opacity:.65;">A</span></button>
        <button id="rejectCurBtn" class="btn btn-outline" style="flex:1;justify-content:center;gap:9px;padding:13px 20px;font-size:14px;">&#10005; Reject <span class="mono" style="font-size:11px;opacity:.6;">R</span></button>
      </div>
      <div class="reason-box" style="border-left-color:${t.color}">
        <div class="reason-kicker">Why it scored ${cur.score}</div>
        <div style="font-size:13.5px;color:var(--tx2);line-height:1.6;">${escapeHtml(cur.reason || "")}</div>
        <div class="row wrap" style="gap:7px;margin-top:11px;">${(cur.keywords || []).map((k) => `<span class="kw-chip">${escapeHtml(k)}</span>`).join("")}</div>
      </div>
      <div style="margin-bottom:20px;">
        <div class="reason-kicker">Your note</div>
        <textarea id="noteInput" rows="2" class="textarea-input" placeholder="Add context for whoever reads the brief…">${escapeHtml(noteVal)}</textarea>
      </div>
      ${
        dups
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
              <a href="${escapeHtml(d.url || "#")}" target="_blank" style="flex:1;min-width:0;font-size:12.5px;color:var(--tx2);">${escapeHtml(d.title)}</a>
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
        peers
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
                <a href="${escapeHtml(p.url || "#")}" target="_blank" style="flex:1;min-width:0;font-size:12.5px;color:var(--tx2);">${escapeHtml(p.title)}</a>
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
          <span class="mono" style="font-size:10px;color:var(--tx4);">${escapeHtml(bodyMeta)}</span>
        </div>
        ${
          cur.body
            ? `<div class="article-body-text">${escapeHtml(cur.body)}</div>`
            : `<div class="body-missing"><span style="color:var(--tx3);">—</span><div style="flex:1;font-size:13px;color:var(--tx2);line-height:1.55;">Body text wasn't retrieved for this article. Read it at the source, or ask an administrator to re-run the text fetch.</div></div>`
        }
      </div>
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
    root.querySelectorAll("[data-sort]").forEach((b) => b.addEventListener("click", () => { s.sort = b.getAttribute("data-sort"); fetchList(); }));
    root.querySelectorAll("[data-tier]").forEach((b) =>
      b.addEventListener("click", () => {
        const k = b.getAttribute("data-tier");
        s.tiers[k] = !s.tiers[k];
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

    const decidedBtn = root.querySelector("#decidedBtn");
    if (decidedBtn) decidedBtn.addEventListener("click", () => { s.showDecided = !s.showDecided; fetchList(); });
    const starredBtn = root.querySelector("#starredBtn");
    if (starredBtn) starredBtn.addEventListener("click", () => { s.starredOnly = !s.starredOnly; fetchList(); });
    const goExport = root.querySelector("#goExportBtn");
    if (goExport) goExport.addEventListener("click", () => navigate("export"));

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

    const resetBtn = root.querySelector("#resetFiltersBtn");
    if (resetBtn) resetBtn.addEventListener("click", resetFilters);
    const retryBtn = root.querySelector("#reviewRetry");
    if (retryBtn) retryBtn.addEventListener("click", fetchList);

    const bulkApprove = root.querySelector("#bulkApprove");
    if (bulkApprove) bulkApprove.addEventListener("click", () => bulk("approve"));
    const bulkReject = root.querySelector("#bulkReject");
    if (bulkReject) bulkReject.addEventListener("click", () => bulk("reject"));
    const bulkStar = root.querySelector("#bulkStar");
    if (bulkStar) bulkStar.addEventListener("click", () => bulk("star"));
    const clearChecked = root.querySelector("#clearChecked");
    if (clearChecked) clearChecked.addEventListener("click", () => { s.checked.clear(); render(); });

    const hideHints = root.querySelector("#hideHints");
    if (hideHints) hideHints.addEventListener("click", () => root.querySelector(".hints-bar")?.remove());

    const starCurBtn = root.querySelector("#starCurBtn");
    if (starCurBtn) starCurBtn.addEventListener("click", () => toggleStar(s.selId));
    const approveCurBtn = root.querySelector("#approveCurBtn");
    if (approveCurBtn) approveCurBtn.addEventListener("click", () => decide(s.selId, "approved"));
    const rejectCurBtn = root.querySelector("#rejectCurBtn");
    if (rejectCurBtn) rejectCurBtn.addEventListener("click", () => decide(s.selId, "rejected"));

    const noteInput = root.querySelector("#noteInput");
    if (noteInput) {
      const debouncedSave = debounce((id, val) => {
        api.patch(`/articles/${id}`, { note: val }).catch(() => {});
      }, 600);
      noteInput.addEventListener("input", (e) => {
        s.noteDrafts[s.selId] = e.target.value;
        debouncedSave(s.selId, e.target.value);
      });
    }

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
      document.removeEventListener("keydown", onKeyDown);
    },
  };
}
