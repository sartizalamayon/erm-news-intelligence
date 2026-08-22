// Maps a screen key (state.screen) to its module's mount function.
// Each screen module exports `mount(root, ctx) -> { destroy() }`.

import { mountDashboard } from "./screens/dashboard.js";
import { mountReview } from "./screens/review.js";
import { mountExport } from "./screens/export.js";
import { mountSearch } from "./screens/search.js";
import { mountSources } from "./screens/sources.js";
import { mountKeywords } from "./screens/keywords.js";
import { mountPrompts } from "./screens/prompts.js";
import { mountModels } from "./screens/models.js";
import { mountManual } from "./screens/manual.js";
import { mountRun } from "./screens/run.js";
import { mountUsers } from "./screens/users.js";

const SCREENS = {
  dash: mountDashboard,
  review: mountReview,
  export: mountExport,
  search: mountSearch,
  sources: mountSources,
  keywords: mountKeywords,
  prompts: mountPrompts,
  models: mountModels,
  manual: mountManual,
  run: mountRun,
  users: mountUsers,
};

export function mountScreen(key, root, ctx) {
  const fn = SCREENS[key] || mountDashboard;
  return fn(root, ctx);
}
