-- 0001_settings.sql
--
-- Key/value config store, carried forward from legacy `settings`
-- (legacy-behavior.md §6: "settings (key/value; only report_cache
-- populated)"). Kept because it is genuinely useful, low-ceremony
-- storage for admin-tunable values that are not one of the typed
-- app.config.Settings env vars (which cover process-boot config, not
-- runtime-admin-editable config) — e.g. the storage ceiling (DoD #4:
-- "ceiling as configuration not a constant") and the OTP-allowed email
-- domain (frontend-contract.md §3.0 domainSuffix).
--
-- Seeded defaults below are safe to override by UPDATE at any time;
-- nothing in app code hardcodes these values (see app/services/storage.py
-- and app/db/settings.py).

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO settings (key, value) VALUES
    ('storage_ceiling_mb', '500'),          -- Supabase free-tier ceiling (ADR §9.4); override once the real plan is known.
    ('raw_retention_days', '90'),           -- full-detail retention window for ALL articles regardless of match status (storage-policy.md §2).
    ('body_retention_days', '60'),          -- article_bodies.content_text retention window (storage-policy.md §2).
    ('allowed_email_domain', 'grameenphone.com')  -- frontend-contract.md §3.0 domainSuffix; OTP/login domain gate.
ON CONFLICT (key) DO NOTHING;
