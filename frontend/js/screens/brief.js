// Daily Risk Insight / brief (frontend-contract.md §3.5).
//
// Expected API (proposal — app/api/routers/brief.py is a stub):
//   GET  /brief/draft?date=YYYY-MM-DD -> { fuel, ready, sections: [...] } | 404 if none drafted yet
//   POST /brief/draft { fuel } -> generates and returns the same shape
//   PUT  /config/fuel-line { fuel } -> persists the fuel line independent of drafting
//   GET  /brief/download.docx?date= -> binary
//
// Step-done fixes per §8 item ("mostly Client — step 1 is hardcoded
// done:true regardless of actual approval state, step 3 never flips"):
// step 1 is derived from state.approvedCount > 0, step 3 from whether a
// download actually happened this session — not hardcoded.

import { API_BASE } from "../config.js";
import { api, ApiError } from "../api.js";
import { state } from "../state.js";
import { escapeHtml, debounce, fmtErr, notWiredBanner, skeletonRows } from "../util.js";

export function mountBrief(root) {
  let alive = true;
  const s = {
    fuel: "",
    loadingDraft: true,
    draftError: null,
    briefReady: false,
    briefBusy: false,
    sections: [],
    downloaded: false,
    savingFuel: false,
  };

  const debouncedSaveFuel = debounce(saveFuel, 700);

  render();
  loadDraft();

  async function loadDraft() {
    s.loadingDraft = true;
    render();
    try {
      const today = new Date().toISOString().slice(0, 10);
      const data = await api.get("/brief/draft", { query: { date: today } });
      if (!alive) return;
      s.fuel = (data && data.fuel) || s.fuel;
      s.sections = (data && data.sections) || [];
      s.briefReady = !!(data && data.ready && s.sections.length);
      s.loadingDraft = false;
      s.draftError = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.loadingDraft = false;
      // A 404 here plausibly means "nothing drafted yet today" rather
      // than "endpoint doesn't exist" — but the stub router 404s for
      // every path, so we can't distinguish; show the not-wired banner
      // either way and let the draft button attempt POST regardless.
      s.draftError = err;
      render();
    }
  }

  function saveFuel() {
    s.savingFuel = true;
    api
      .put("/config/fuel-line", { fuel: s.fuel })
      .catch(() => {})
      .finally(() => {
        s.savingFuel = false;
      });
  }

  async function makePreview() {
    s.briefBusy = true;
    render();
    try {
      const data = await api.post("/brief/draft", { fuel: s.fuel });
      s.sections = (data && data.sections) || [];
      s.briefReady = true;
      s.draftError = null;
    } catch (err) {
      s.draftError = err;
    }
    s.briefBusy = false;
    render();
  }

  async function downloadBrief() {
    try {
      const today = new Date().toISOString().slice(0, 10);
      const res = await fetch(API_BASE + "/brief/download.docx?date=" + today, { credentials: "include" });
      if (!res.ok) throw new ApiError("Download failed (" + res.status + ")", { status: res.status });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "daily-risk-insight-" + today + ".docx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      s.downloaded = true;
      render();
    } catch (err) {
      window.alert("Couldn't download the brief: " + fmtErr(err));
    }
  }

  function render() {
    const approvedCount = state.approvedCount;
    const steps = [
      { n: "1", label: "Approve stories", done: !!approvedCount },
      { n: "2", label: "Draft the brief", done: s.briefReady },
      { n: "3", label: "Download .docx", done: s.downloaded },
    ];

    root.innerHTML = `
      <div class="page page-narrow" style="max-width:880px;">
        <div class="row" style="gap:12px;margin-bottom:22px;">
          ${steps
            .map(
              (st) => `<div class="card-sm" style="flex:1;padding:14px 16px;border:1px solid ${st.done ? "var(--acline)" : "var(--line)"};border-radius:12px;background:${
                st.done ? "var(--acsoft)" : "var(--panel2)"
              };">
            <div class="row" style="gap:9px;">
              <span style="width:22px;height:22px;flex:0 0 22px;border-radius:50%;display:grid;place-items:center;font-size:10.5px;font-weight:600;background:${
                st.done ? "var(--acbtn)" : "var(--gridline)"
              };color:${st.done ? "var(--onac)" : "var(--tx3)"};" class="mono">${st.n}</span>
              <span style="font-size:13px;font-weight:600;color:${st.done ? "var(--tx)" : "var(--tx3)"};">${st.label}</span>
            </div>
          </div>`
            )
            .join("")}
        </div>

        <div class="card" style="margin-bottom:16px;">
          <h3>Fuel price line</h3>
          <p class="hint">Carried under Macroeconomic Outlook every day. Edit before drafting.</p>
          ${s.loadingDraft ? skeletonRows(1, 40) : `<input id="fuelInput" class="text-input" value="${escapeHtml(s.fuel)}" style="font-size:13.5px;padding:11px 13px;" />`}
          ${s.savingFuel ? `<div style="font-size:11px;color:var(--tx3);margin-top:6px;">Saving…</div>` : ""}
        </div>

        <div class="card" style="margin-bottom:16px;">
          <div class="row-between wrap" style="align-items:flex-end;gap:20px;">
            <div>
              <h3>Draft from approved stories</h3>
              <p class="hint" style="margin:0;"><b style="color:var(--tx);">${
                approvedCount !== undefined ? approvedCount : "—"
              }</b> approved today across 5 sections. The model writes a first draft in house style — you polish after download.</p>
            </div>
            <button id="makePreviewBtn" class="btn btn-primary" style="padding:11px 18px;white-space:nowrap;" ${s.briefBusy ? "disabled" : ""}>${
      s.briefBusy ? "Drafting…" : s.briefReady ? "Redraft" : "Draft the brief"
    }</button>
          </div>
        </div>

        ${
          s.draftError && !s.briefReady
            ? s.draftError.kind === "not_found"
              ? notWiredBanner("POST /brief/draft")
              : `<div class="banner banner-error"><span>&#10005;</span><div>${escapeHtml(fmtErr(s.draftError))}</div></div>`
            : ""
        }

        ${s.briefReady ? renderPreview() : ""}
      </div>
    `;
    bind();
  }

  function renderPreview() {
    return `<div class="card">
      <div class="row-between wrap" style="margin-bottom:16px;">
        <h3 style="margin:0;">Preview</h3>
        <div class="row" style="gap:9px;">
          <button id="regenBtn" class="btn btn-outline" style="padding:9px 15px;font-size:12.5px;">Regenerate</button>
          <button id="downloadBtn" class="btn btn-primary" style="padding:9px 15px;font-size:12.5px;">&#8595; Download .docx</button>
        </div>
      </div>
      <div style="background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:28px 32px;">
        <div style="font-size:19px;font-weight:700;letter-spacing:-.5px;">Daily Risk Insights | ${escapeHtml(todayLabel())}</div>
        <div style="height:2px;background:var(--acbtn);margin:11px 0 20px;"></div>
        ${s.sections
          .map(
            (sec) => `<div style="margin-bottom:20px;">
          <div style="font-size:14px;font-weight:700;letter-spacing:-.2px;color:var(--tx);">${escapeHtml(sec.title)}</div>
          ${sec.fuelLine ? `<div style="color:var(--actx);font-weight:600;font-size:12.5px;margin-top:6px;">${escapeHtml(sec.fuelLine)}</div>` : ""}
          ${(sec.bullets || [])
            .map(
              (b) => `<div class="row" style="gap:10px;margin-top:9px;align-items:flex-start;">
              <span style="flex:0 0 auto;color:var(--tx4);line-height:1.6;">&#8226;</span>
              <div style="flex:1;font-size:13px;line-height:1.62;color:var(--tx2);"><b style="color:var(--tx);font-weight:600;">${escapeHtml(
                b.lead
              )}</b> – ${escapeHtml(b.rest)}</div>
            </div>`
            )
            .join("")}
          ${sec.empty || !(sec.bullets || []).length ? `<div style="font-size:12.5px;color:var(--tx3);margin-top:7px;">No approved articles in this section.</div>` : ""}
        </div>`
          )
          .join("")}
      </div>
    </div>`;
  }

  function todayLabel() {
    return new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }

  function bind() {
    const fuelInput = root.querySelector("#fuelInput");
    if (fuelInput)
      fuelInput.addEventListener("input", (e) => {
        s.fuel = e.target.value;
        debouncedSaveFuel();
      });
    const makePreviewBtn = root.querySelector("#makePreviewBtn");
    if (makePreviewBtn) makePreviewBtn.addEventListener("click", makePreview);
    const regenBtn = root.querySelector("#regenBtn");
    if (regenBtn) regenBtn.addEventListener("click", makePreview);
    const downloadBtn = root.querySelector("#downloadBtn");
    if (downloadBtn) downloadBtn.addEventListener("click", downloadBrief);
  }

  return {
    destroy() {
      alive = false;
    },
  };
}
