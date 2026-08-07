// How it works / manual (frontend-contract.md §3.11).
//
// Predominantly static explainer prose — no live data screen, per the
// contract's own note: "almost everything here is either genuinely
// static help copy... or a handful of summary stats that duplicate
// numbers shown live elsewhere." The illustrative walkthrough numbers
// below (5,012 fetched, etc.) are the same worked example the design
// uses and are labelled "(example day)" rather than presented as live
// counts, since /dashboard/today doesn't exist yet to source them for
// real — see this task's final report for that gap.

import { escapeHtml } from "../util.js";

const TABS = [
  { n: 1, l: "1 · Collect" },
  { n: 2, l: "2 · Keyword filter" },
  { n: 3, l: "3 · AI stage" },
  { n: 4, l: "4 · Review & export" },
  { n: 5, l: "Roles & glossary" },
];

export function mountManual(root) {
  const s = { phase: 1 };
  render();

  function render() {
    root.innerHTML = `
      <div class="page page-md">
        <div class="card" style="margin-bottom:18px;">
          <h2 style="font-size:24px;font-weight:700;letter-spacing:-1px;margin:0 0 8px;">Five thousand articles in, twenty-five out.</h2>
          <p style="color:var(--tx2);font-size:14px;line-height:1.65;margin:0;max-width:66ch;">Every weekday the desk narrows the whole day's news down to the handful of stories that matter to enterprise risk. It happens in four phases. Nothing is ever deleted — each phase only decides what gets your attention.</p>
        </div>

        <div class="phase-tabs">
          ${TABS.map((t) => `<button class="chip-toggle ${s.phase === t.n ? "active" : ""}" data-phase="${t.n}">${t.l}</button>`).join("")}
        </div>

        ${panel()}
      </div>
    `;
    bind();
  }

  function panel() {
    if (s.phase === 1) return phase1();
    if (s.phase === 2) return phase2();
    if (s.phase === 3) return phase3();
    if (s.phase === 4) return phase4();
    return phase5();
  }

  function phaseHead(n, chip, title) {
    return `<div class="row" style="gap:12px;margin-bottom:6px;">
      <span style="width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-size:13px;font-weight:700;color:var(--onac);background:${chip};" class="mono">${n}</span>
      <h3 style="font-size:20px;font-weight:600;letter-spacing:-.6px;margin:0;">${title}</h3>
    </div>`;
  }

  function phase1() {
    const methods = [
      { l: "Sitemap", n: 18, w: "100%", note: "The paper publishes a machine-readable list of today's articles." },
      { l: "RSS feed", n: 9, w: "50%", note: "A standard news feed. Usually only the most recent items." },
      { l: "Google News", n: 11, w: "61%", note: "Used where a site blocks us directly. A dated subset, not everything." },
      { l: "JSON API", n: 5, w: "28%", note: "The site's own data endpoint. Most complete when available." },
      { l: "ID scan", n: 3, w: "17%", note: "Walks article numbers one by one. Slow but misses nothing." },
    ];
    const risks = [
      { icon: "&#10005;", color: "var(--red)", title: "The site blocks us", note: "Shows as an error on Source health. Usually fixed by switching that source to Google News." },
      { icon: "&#10005;", color: "var(--red)", title: "No readable date", note: "The article is dropped because we can't tell if it's from today. Needs a parser fix." },
      { icon: "&#9680;", color: "var(--amber)", title: "Quiet day", note: "Nothing published, nothing wrong. The page says “no articles today” rather than an error." },
    ];
    return `<div class="card">
      ${phaseHead(1, "var(--tx4)", "Collect — about 5,000 articles a day (example day)")}
      <p style="color:var(--tx2);font-size:13.5px;line-height:1.7;margin:0 0 22px;max-width:70ch;">Every morning the collector visits every configured news source and asks each one: what did you publish today? It takes whatever it gets — politics, sport, entertainment, everything. No judgement happens here. The only thing thrown away at this stage is an article with no usable publication date, or one already stored from a previous run.</p>
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:22px;">
        <div style="border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:18px 20px;">
          <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:14px;">How each source is read (example)</div>
          <div class="stack" style="gap:12px;">
            ${methods
              .map(
                (m) => `<div>
              <div class="row-between" style="margin-bottom:5px;"><span style="font-size:12.5px;color:var(--tx2);">${m.l}</span><span class="mono" style="font-size:11px;color:var(--tx3);">${m.n} sources</span></div>
              <div class="bar-track bar-track-sm"><div class="bar-fill" style="width:${m.w};background:var(--ac);"></div></div>
              <div style="font-size:11px;color:var(--tx3);margin-top:5px;line-height:1.45;">${m.note}</div>
            </div>`
              )
              .join("")}
          </div>
        </div>
        <div style="border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:18px 20px;">
          <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:14px;">What can go wrong</div>
          <div class="stack" style="gap:12px;">
            ${risks
              .map(
                (r) => `<div class="row" style="gap:11px;align-items:flex-start;">
              <span style="flex:0 0 auto;color:${r.color};font-size:12px;">${r.icon}</span>
              <div style="flex:1;"><div style="font-size:12.5px;font-weight:600;color:var(--tx);">${r.title}</div><div style="font-size:11.5px;color:var(--tx3);line-height:1.5;margin-top:3px;">${r.note}</div></div>
            </div>`
              )
              .join("")}
          </div>
        </div>
      </div>
      <div class="banner banner-info" style="margin-bottom:0;">
        <span>i</span>
        <div style="flex:1;">If a source shows <b style="color:var(--tx);">no articles today</b> on the Source health page, that usually means a quiet news day — not a fault. The page tells you which of the two it is.</div>
      </div>
    </div>`;
  }

  function phase2() {
    const cards = [
      { n: 41, l: "Telco / regulatory" },
      { n: 38, l: "Macroeconomy" },
      { n: 44, l: "Geopolitical" },
      { n: 31, l: "Political" },
      { n: 29, l: "Others" },
    ];
    return `<div class="card">
      ${phaseHead(2, "var(--ac)", "Keyword filter — 5,000 down to about 1,100 (example day)")}
      <p style="color:var(--tx2);font-size:13.5px;line-height:1.7;margin:0 0 22px;max-width:70ch;">Each article is checked against the keyword list, in English and Bengali, grouped into five risk sections. If any term appears in the headline or the body, the article is kept and tagged with the section that term belongs to. If none appear, it stops here. This is a plain text match: no judgement, no model, no guessing.</p>
      <div style="border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:20px 22px;margin-bottom:18px;">
        <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:14px;">A match, step by step</div>
        <div class="serif" style="font-size:17px;line-height:1.5;color:var(--tx);margin-bottom:14px;">BTRC moves to reprice <mark style="background:var(--acsoft);color:var(--actx);padding:1px 5px;border-radius:4px;font-weight:600;">spectrum</mark> ahead of the next <mark style="background:var(--acsoft);color:var(--actx);padding:1px 5px;border-radius:4px;font-weight:600;">auction</mark> round</div>
        <div class="row wrap" style="gap:12px;font-size:12.5px;color:var(--tx2);">
          <span class="mono" style="font-size:11px;color:var(--actx);background:var(--acsoft);padding:3px 9px;border-radius:5px;">spectrum</span>
          <span style="color:var(--tx4);">+</span>
          <span class="mono" style="font-size:11px;color:var(--actx);background:var(--acsoft);padding:3px 9px;border-radius:5px;">auction</span>
          <span style="color:var(--tx4);">&rarr;</span>
          <span>kept, tagged <b style="color:var(--tx);">Telco/regulatory</b></span>
        </div>
        <div class="section-divider"></div>
        <div class="serif" style="font-size:17px;line-height:1.5;color:var(--tx3);margin-bottom:12px;">Bangladesh beat Sri Lanka by six wickets in Colombo</div>
        <div class="row" style="gap:12px;font-size:12.5px;color:var(--tx3);">
          <span class="mono" style="font-size:11px;color:var(--tx3);background:var(--gridline);padding:3px 9px;border-radius:5px;">no term found</span>
          <span style="color:var(--tx4);">&rarr;</span>
          <span>stops here, never reaches the queue</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;">
        ${cards
          .map(
            (c) => `<div style="border:1px solid var(--line);border-radius:11px;background:var(--panel2);padding:15px 16px;">
          <div style="font-size:20px;font-weight:700;letter-spacing:-.8px;color:var(--ac2);">${c.n}</div>
          <div style="font-size:11.5px;color:var(--tx2);margin-top:6px;line-height:1.4;">${c.l}</div>
        </div>`
          )
          .join("")}
      </div>
    </div>`;
  }

  function phase3() {
    const steps = [
      { g: "1", chip: "linear-gradient(145deg,#8b6cff,#b39cff)", name: "Translate", what: "Every Bengali headline gets an English version stored alongside it, so one reviewer can work the whole queue.", ex: "ডলারের বিপরীতে টাকার মান কমল → Taka weakens against the dollar" },
      { g: "2", chip: "linear-gradient(145deg,#1a9bd0,#36d6f5)", name: "Group duplicates", what: "When several outlets run the same story, they collapse into one card. You decide once instead of eight times.", ex: "8 articles about the Hormuz strike → 1 card, badge “+7”" },
      { g: "3", chip: "linear-gradient(145deg,#c0392b,#e2685a)", name: "Score relevance", what: "Each story is rated 1–10 for how much it matters to enterprise risk, with a one-line reason you can read and disagree with.", ex: "Spectrum repricing → 9/10, “direct regulatory action on pricing”" },
      { g: "4", chip: "linear-gradient(145deg,#d97706,#fbbf24)", name: "Group topics", what: "Related stories are clustered by theme so a running story reads as one thread rather than scattered cards.", ex: "“Energy costs” gathers fuel imports, gas curtailment, Brent moves" },
    ];
    const bands = [
      { l: "High", b: "8–10", c: "var(--red)", bg: "var(--redsoft)", m: "Acts on us directly, or a shock big enough to reprice costs. Read today.", ex: "BTRC reprices spectrum" },
      { l: "Medium", b: "6–7", c: "var(--amber)", bg: "var(--ambersoft)", m: "Affects us through a second step — energy, banks, trade. Worth a line in the brief.", ex: "Fuel import bill climbs 14%" },
      { l: "Low", b: "5", c: "var(--green)", bg: "var(--greensoft)", m: "Context. Useful background, rarely actionable on the day.", ex: "Chip bottleneck lifts handset prices" },
      { l: "Least", b: "below 5", c: "var(--violet)", bg: "var(--violetsoft)", m: "Matched a keyword but reads as unrelated. Usually a reject.", ex: "Foreign data rule with no local effect" },
    ];
    return `<div class="card">
      ${phaseHead(3, "var(--ac3)", "AI stage — about 1,100 organised into a reviewable set")}
      <p style="color:var(--tx2);font-size:13.5px;line-height:1.7;margin:0 0 22px;max-width:70ch;">A language model does four jobs in order. It never removes an article from the database — it translates, groups and scores, so the queue arrives sorted with the most consequential story at the top.</p>
      <div class="stack" style="gap:10px;margin-bottom:24px;">
        ${steps
          .map(
            (a) => `<div class="row" style="gap:14px;padding:16px 18px;border:1px solid var(--line);border-radius:12px;background:var(--panel2);align-items:flex-start;">
          <span style="flex:0 0 32px;height:32px;border-radius:9px;display:grid;place-items:center;font-size:13px;font-weight:700;color:var(--onac);background:${a.chip};">${a.g}</span>
          <div style="flex:1;min-width:0;">
            <div style="font-size:14px;font-weight:600;letter-spacing:-.2px;">${a.name}</div>
            <div style="font-size:12.5px;color:var(--tx2);line-height:1.6;margin-top:5px;">${a.what}</div>
            <div class="mono" style="font-size:11px;color:var(--tx3);margin-top:7px;">${escapeHtml(a.ex)}</div>
          </div>
        </div>`
          )
          .join("")}
      </div>
      <div style="border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:20px 22px;margin-bottom:18px;">
        <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:6px;">How the 1–10 score becomes a colour</div>
        <p style="font-size:12.5px;color:var(--tx2);line-height:1.6;margin:0 0 18px;max-width:64ch;">You never need to read the number. The colour is the whole message — and you can filter the queue by colour.</p>
        <div class="stack" style="gap:10px;">
          ${bands
            .map(
              (b) => `<div class="row" style="gap:14px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel);">
            <span class="pill" style="flex:0 0 auto;background:${b.bg};color:${b.c};"><span class="pill-dot" style="background:${b.c}"></span>${b.l}</span>
            <span class="mono" style="flex:0 0 46px;font-size:12px;color:var(--tx3);">${b.b}</span>
            <span style="flex:1;min-width:0;font-size:12.5px;color:var(--tx2);line-height:1.5;">${b.m}</span>
            <span class="serif" style="flex:0 0 auto;font-size:12.5px;color:var(--tx3);font-style:italic;max-width:38%;text-align:right;line-height:1.45;">${b.ex}</span>
          </div>`
            )
            .join("")}
        </div>
      </div>
      <div class="banner" style="background:var(--ambersoft);border:1px solid var(--ambersoft);margin-bottom:0;">
        <span style="color:var(--amber);">&#9888;</span>
        <div style="flex:1;color:var(--tx2);">The score is advice, not a decision. A low score still appears in the queue and can still be approved. If the model keeps misjudging a kind of story, an administrator adjusts the guidance on the <b style="color:var(--tx);">AI prompts</b> page — that is how the tool gets sharper over time.</div>
      </div>
    </div>`;
  }

  function phase4() {
    const shortcuts = [
      ["J / K", "Move down and up the queue"],
      ["A", "Approve the open story"],
      ["R", "Reject it"],
      ["X", "Tick it for a bulk action"],
      ["S", "Star it to come back later"],
      ["/", "Jump to the filter box"],
    ];
    const outputs = [
      ["&#9638;", "Excel export", "Build it on the Export page — approved only, top N by relevance, a date range, or the whole matched pool."],
      ["&#9998;", "Word brief", "Daily Risk Insights, drafted from approved stories in house style. You polish the wording after download."],
      ["&#9733;", "Starred list", "A running shelf of stories to revisit, separate from approve and reject."],
    ];
    return `<div class="card">
      ${phaseHead(4, "var(--green)", "Review and export — your decisions")}
      <p style="color:var(--tx2);font-size:13.5px;line-height:1.7;margin:0 0 22px;max-width:70ch;">This is the only phase where a person decides anything. You work down the queue approving or rejecting; approved stories become the Excel export and the Word brief. Nothing leaves the tool that you didn't approve.</p>
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:18px;">
        <div style="border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:18px 20px;">
          <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:14px;">Working the queue fast</div>
          <div class="stack" style="gap:9px;">
            ${shortcuts.map(([k, w]) => `<div class="row" style="gap:12px;"><b class="hint-key" style="flex:0 0 52px;text-align:center;">${k}</b><span style="flex:1;font-size:12.5px;color:var(--tx2);">${w}</span></div>`).join("")}
          </div>
        </div>
        <div style="border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:18px 20px;">
          <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:14px;">What comes out</div>
          <div class="stack" style="gap:12px;">
            ${outputs
              .map(
                ([icon, title, note]) => `<div class="row" style="gap:11px;align-items:flex-start;">
              <span style="flex:0 0 auto;color:var(--actx);font-size:12px;">${icon}</span>
              <div style="flex:1;"><div style="font-size:12.5px;font-weight:600;color:var(--tx);">${title}</div><div style="font-size:11.5px;color:var(--tx3);line-height:1.5;margin-top:3px;">${note}</div></div>
            </div>`
              )
              .join("")}
          </div>
        </div>
      </div>
      <div style="border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:20px 22px;">
        <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:14px;">Grouped stories</div>
        <p style="font-size:12.5px;color:var(--tx2);line-height:1.7;margin:0 0 14px;max-width:68ch;">When several outlets run the same story, you see <b style="color:var(--tx);">one</b> card with a <b style="color:var(--tx);">+N</b> badge. Approving the card approves the story. Open the group only if you'd rather export a different outlet's version — press <b style="color:var(--tx);">keep</b> on that one.</p>
        <div class="row" style="gap:14px;padding:14px 16px;border:1px solid var(--line);border-radius:10px;background:var(--panel);">
          <span class="tag">Al Jazeera</span>
          <span class="serif" style="flex:1;font-size:13.5px;color:var(--tx);">Shipping insurers pull cover for Hormuz transits</span>
          <span class="pill" style="background:var(--acsoft);color:var(--actx);">+7</span>
        </div>
      </div>
    </div>`;
  }

  function phase5() {
    const roles = [
      {
        name: "Analyst",
        icon: "&#9673;",
        chip: "var(--acbtn)",
        border: "var(--acline)",
        bg: "var(--acsoft)",
        can: [
          ["&#10003;", "var(--green)", "See the dashboard, short and long term"],
          ["&#10003;", "var(--green)", "Work the review queue — approve, reject, star, annotate"],
          ["&#10003;", "var(--green)", "Build exports and draft the daily brief"],
          ["&#10003;", "var(--green)", "Search the whole archive"],
          ["&#10005;", "var(--red)", "Cannot change keywords, sources or AI prompts"],
        ],
      },
      {
        name: "Administrator",
        icon: "&#9881;",
        chip: "var(--tx4)",
        border: "var(--line)",
        bg: "var(--panel2)",
        can: [
          ["&#10003;", "var(--green)", "Everything an analyst can do"],
          ["&#10003;", "var(--green)", "Add, edit, disable and test news sources"],
          ["&#10003;", "var(--green)", "Own the keyword list — the whole of phase 2"],
          ["&#10003;", "var(--green)", "Write the AI guidance and scoring examples"],
          ["&#10003;", "var(--green)", "Re-run any pipeline stage and read source health"],
        ],
      },
    ];
    const glossary = [
      ["Phase", "One of the four stages a story passes through: collect, keyword filter, AI, review."],
      ["Matched", "An article that contained at least one of your keywords. Everything in the queue is matched."],
      ["Grouped", "Several outlets' versions of one story folded into a single card, shown as “+N”."],
      ["Relevance", "The 1–10 rating from the AI stage, shown to you as a colour: High, Medium, Low, Least."],
      ["Topic", "A theme that links otherwise separate stories, e.g. “Energy costs”."],
      ["Approved", "You signed it off. Approved stories are the only ones that reach the brief."],
    ];
    return `<div class="card">
      <h3 style="font-size:20px;font-weight:600;letter-spacing:-.6px;margin:0 0 6px;">Who can do what</h3>
      <p style="color:var(--tx2);font-size:13.5px;line-height:1.7;margin:0 0 22px;max-width:70ch;">Two kinds of account. Analysts read and decide; administrators also own the configuration that shapes what analysts see.</p>
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:22px;">
        ${roles
          .map(
            (r) => `<div style="border:1px solid ${r.border};border-radius:12px;background:${r.bg};padding:20px 22px;">
          <div class="row" style="gap:10px;margin-bottom:14px;">
            <span style="width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font-size:12px;color:var(--onac);background:${r.chip};">${r.icon}</span>
            <div style="font-size:15px;font-weight:600;letter-spacing:-.3px;">${r.name}</div>
          </div>
          <div class="stack" style="gap:8px;">
            ${r.can.map(([mark, color, what]) => `<div class="row" style="gap:10px;align-items:flex-start;"><span style="flex:0 0 auto;font-size:11px;color:${color};">${mark}</span><span style="flex:1;font-size:12.5px;color:var(--tx2);line-height:1.5;">${what}</span></div>`).join("")}
          </div>
        </div>`
          )
          .join("")}
      </div>
      <div style="border:1px solid var(--line);border-radius:12px;background:var(--panel2);padding:20px 22px;">
        <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:14px;">Words used around here</div>
        <div class="stack" style="gap:11px;">
          ${glossary.map(([term, def]) => `<div class="row" style="gap:14px;align-items:flex-start;"><span style="flex:0 0 128px;font-size:12.5px;font-weight:600;color:var(--tx);">${term}</span><span style="flex:1;font-size:12.5px;color:var(--tx2);line-height:1.55;">${def}</span></div>`).join("")}
        </div>
      </div>
    </div>`;
  }

  function bind() {
    root.querySelectorAll("[data-phase]").forEach((b) =>
      b.addEventListener("click", () => {
        s.phase = Number(b.getAttribute("data-phase"));
        render();
      })
    );
  }

  return {
    destroy() {},
  };
}
