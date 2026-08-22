-- 0026_watch_entities.sql
--
-- Watchlist entities added by an operator instead of by a code change.
--
-- app/db/watchlist.py argues at length that adding an entity SHOULD be
-- a code change, and the argument is not decoration -- it is three
-- measured failures in this corpus:
--
--   * the bare Bangla token রবি matched 222 articles, two of which were
--     the operator; the rest were রবিবার (Sunday) and রবিউল (a name),
--   * তরঙ্গ ("spectrum") is a substring of অন্তরঙ্গ ("intimate") and
--     pulled a run of celebrity stories into a spectrum-policy row,
--   * নগদ ("cash") and বিকাশ ("development") each collect ~60 headlines
--     of which roughly two thirds are not the company.
--
-- Postgres offers no `\y` word boundary that works for Bangla
-- (`'রবিবার' ~ '\yরবি\y'` is TRUE), so a self-service text field is
-- genuinely capable of re-importing all three problems.
--
-- The owner asked for self-service anyway. The guard rail therefore
-- MOVES rather than disappearing: app/services/watchlist_admin.py runs
-- a proposed pattern against the real corpus and shows the operator its
-- matches -- both unguarded and guarded by a telecom/MFS context cue,
-- with both totals -- BEFORE any row lands in this table. The eyeball
-- pass that used to happen in code review happens in the preview. This
-- table stores only what survived that pass.
--
-- ── WHY RULES ARE A CHILD TABLE AND NOT A JSONB COLUMN ──────────────
-- `sources.config` (0002) is JSONB and rightly so: its keys differ per
-- fetch adapter and the database never reasons about them. Rules are
-- the opposite case on every axis that matters here.
--
--   1. `pattern` is the only column in this schema whose contents are
--      handed to Postgres as a regex operand. It is always BOUND as a
--      query parameter and never string-interpolated (see
--      app/db/watchlist.py::entity_where_sql), but it still deserves
--      the strongest at-rest guarantees available, and those are column
--      constraints: NOT NULL, non-empty, length-capped. A JSONB
--      document can carry `{"pattern": null}` or a one-megabyte string
--      and the database cannot say no.
--   2. Every rule has exactly the same two fields, and will keep having
--      them: `WatchRule(pattern, context)` is a frozen dataclass. There
--      is no per-entity schema variation for JSONB to absorb.
--   3. Nothing reads a rule set as an opaque document. Every consumer
--      turns one rule into one OR-arm of a WHERE clause, which is a
--      row-shaped operation.
--
-- The cost is one extra join in one query (`custom_entities()`), on a
-- table bounded by how many entities a human will ever type.
--
-- ── WHY KEY COLLISION IS NOT A DB CONSTRAINT ───────────────────────
-- `UNIQUE (key)` below stops one custom entity shadowing another. It
-- cannot stop a custom entity shadowing a CODE-defined one ('btrc',
-- 'grameenphone', ...), because those keys live in Python tuples in
-- app/db/watchlist.py and Postgres cannot see them. A CHECK listing
-- today's fifteen keys would be a copy of the code that goes stale the
-- moment someone adds an entity -- and fixing it would mean editing a
-- shipped migration, which app/db/migrate.py explicitly refuses (it
-- checksums applied files). So the check lives in
-- app/services/watchlist_admin.py::create, which reads the real tuple.
--
-- Defence in depth for the hand-written INSERT that bypasses the
-- service: app/db/watchlist.py::custom_entities DROPS any row whose key
-- matches a code-defined entity and logs it. Without that, two entities
-- would share one key and collide on rollup_monthly_watch's
-- (month, group_key, entity) primary key, and one would silently
-- overwrite the other's counts.
--
-- `group_key` carries the same limitation for the same reason (groups
-- are code too) and gets the same treatment: validated in create(),
-- rows with an unknown group skipped by custom_entities().

CREATE TABLE watch_entities (
    id          BIGSERIAL PRIMARY KEY,
    group_key   TEXT NOT NULL,          -- app/db/watchlist.py WatchGroup.key ('operators' / 'regulatory' / 'mfs'). No FK: groups are code, not rows.
    key         TEXT NOT NULL UNIQUE,   -- stable identifier, also the `entity` value written to rollup_monthly_watch (0021). Derived from the label by the service, never typed.
    label       TEXT NOT NULL,          -- what the dashboard prints, e.g. 'Airtel / Cirkle'.
    note        TEXT NOT NULL DEFAULT '',   -- why this entity is on the register, and what its pattern deliberately excludes. Same role as WatchEntity.note.
    created_by  TEXT NOT NULL DEFAULT '',   -- users.email, NOT users.id -- the convention articles.reviewed_by (0003) and 0023's header already establish. No FK, so deactivating or deleting an account never erases who added a risk entity.
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The key ends up in a dashboard payload and in a rollup primary
    -- key, so it is constrained to the same shape the code-defined keys
    -- have: lowercase, starts with a letter, 2-63 characters.
    CONSTRAINT watch_entities_key_shape CHECK (key ~ '^[a-z][a-z0-9_]{1,62}$'),
    CONSTRAINT watch_entities_label_present CHECK (label <> '' AND length(label) <= 120),
    CONSTRAINT watch_entities_group_present CHECK (group_key <> '')
);
-- Intended query: merge the custom entities into the code-defined ones
-- for the dashboard and the rollup (app/db/watchlist.py) --
--   SELECT e.*, r.pattern, r.context FROM watch_entities e
--   LEFT JOIN watch_entity_rules r ON r.entity_id = e.id
--   ORDER BY e.group_key, e.key, r.position;
-- No index beyond the PK and UNIQUE(key): this table is bounded by how
-- many entities a risk officer will type, the query above reads all of
-- them, and a seq scan over a few dozen rows beats maintaining an index
-- nobody's plan would choose.

CREATE TABLE watch_entity_rules (
    id         BIGSERIAL PRIMARY KEY,
    entity_id  BIGINT NOT NULL REFERENCES watch_entities (id) ON DELETE CASCADE,
    position   INTEGER NOT NULL DEFAULT 0,  -- rule order within the entity, so the stored pattern set reads back exactly as the preview showed it.
    pattern    TEXT NOT NULL,               -- Postgres POSIX regex, matched against watchlist.NORMALIZED_TEXT_SQL. Written by the service from ESCAPED user terms; see _escape_term there.
    context    TEXT,                        -- NULL = unguarded. Otherwise a SECOND pattern that must match the same headline (WatchRule.context). Always one of the code-defined cue sets, never user text.

    -- The length caps are the at-rest half of the injection defence.
    -- The service caps terms before it ever builds a pattern (12 terms
    -- x 60 characters per script); these numbers sit above that so a
    -- legitimate pattern always fits, and far below anything that could
    -- be a pasted regex bomb.
    CONSTRAINT watch_entity_rules_pattern_sane CHECK (pattern <> '' AND length(pattern) <= 2000),
    CONSTRAINT watch_entity_rules_context_sane CHECK (context IS NULL OR (context <> '' AND length(context) <= 4000)),
    CONSTRAINT watch_entity_rules_position_unique UNIQUE (entity_id, position)
);
-- Intended query: the rules for one entity, in order --
--   SELECT pattern, context FROM watch_entity_rules WHERE entity_id = ? ORDER BY position;
-- Served by the index behind UNIQUE (entity_id, position); a separate
-- index on entity_id alone would be redundant with its leading column.
--
-- An entity with zero rules is representable here and matches nothing
-- (entity_where_sql returns `false` for it). The service never creates
-- one -- create() refuses empty terms -- and the FK cascade removes
-- rules with their entity, so the state only arises from a hand-written
-- DELETE.
