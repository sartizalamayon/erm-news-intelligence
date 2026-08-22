-- 0023_user_status.sql
--
-- Admin user management (deactivate). `users` had no way to disable an
-- account short of deleting the row outright (losing audit history —
-- reviewed_by/created_by references elsewhere in the schema point at
-- users.email, not users.id, so a delete would orphan nothing, but is
-- still irreversible and not what "deactivate" means). A boolean flag
-- is enforced at both auth entry points (app/services/auth.py's
-- `login()` and `get_session_user()`) so a deactivated account's
-- existing session stops working immediately, not just future logins.
ALTER TABLE users
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE;
