// Minimal global store — just enough shared state for the app shell
// (auth/session, theme, current route, sidebar collapse). Per-screen UI
// state (filters, selections, drafts) lives inside each screen module's
// own closure instead, since only one screen is mounted at a time; this
// keeps the store small and avoids reimplementing a framework.

const listeners = new Set();

export const state = {
  // Auth/session — populated from GET /me (frontend-contract.md §2).
  user: null, // { email, display_name, initials, role, is_admin }
  authChecked: false,

  theme: "dark",
  railOpen: true,

  screen: "login", // one of the 11 screen keys
  dashMode: "short",

  progress: "", // active job/run status text, shell header (§2)
};

export function setState(patch) {
  Object.assign(state, patch);
  notify();
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function notify() {
  for (const fn of listeners) fn(state);
}

export const SCREEN_TITLES = {
  dash: ["Dashboard", null], // kicker computed from dashMode
  review: ["Review queue", "Keyword-matched stories awaiting a decision"],
  brief: ["Daily Risk Insight", "Draft and download the daily brief"],
  export: ["Export", "Build an Excel or CSV extract"],
  search: ["Search archive", "Every article ever collected"],
  manual: ["How it works", "The four phases, explained"],
  sources: ["Sources", "Administration · outlets and methods"],
  keywords: ["Keywords", "Administration · bilingual search terms"],
  prompts: ["AI prompts", "Administration · scoring guidance and examples"],
  health: ["Source health", "Administration · fetch status and run history"],
};

export const NAV_STRUCTURE = [
  {
    label: "Daily desk",
    items: [
      { key: "dash", label: "Dashboard", icon: "◫" },
      { key: "review", label: "Review queue", icon: "▤", badgeKey: "pendingCount" },
      { key: "brief", label: "Daily Risk Insight", icon: "✎" },
      { key: "export", label: "Export", icon: "↓" },
      { key: "search", label: "Search archive", icon: "⌕" },
    ],
  },
  {
    label: "Administration",
    adminOnly: true,
    items: [
      { key: "sources", label: "Sources", icon: "⌸" },
      { key: "keywords", label: "Keywords", icon: "⌗" },
      { key: "prompts", label: "AI prompts", icon: "◈" },
      { key: "health", label: "Source health", icon: "⚙" },
    ],
  },
  {
    label: "Help",
    items: [{ key: "manual", label: "How it works", icon: "?" }],
  },
];
