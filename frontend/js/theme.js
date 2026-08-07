// Theming (frontend-contract.md §6). 100% client-side: no server value
// anywhere decides dark vs light. Persisted to localStorage — the
// prototype always booted to 'dark' and never persisted (an open
// question per §8/§15); persisting across reloads is the more useful
// real-app behavior, so that's the judgment call made here.

const KEY = "erm_theme";

export function getTheme() {
  try {
    return localStorage.getItem(KEY) || "dark";
  } catch (err) {
    return "dark";
  }
}

export function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(KEY, theme);
  } catch (err) {
    /* ignore */
  }
}

export function themeGlyph(theme) {
  return theme === "dark" ? "☀" : "☾";
}
