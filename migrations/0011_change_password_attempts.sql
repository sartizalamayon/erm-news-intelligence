-- 0011_change_password_attempts.sql
--
-- AUTH-V2 addendum: an already-authenticated user can now rotate their
-- own password (POST /auth/password/change, app/services/auth.py's
-- change_password()). That endpoint still requires the CURRENT
-- password -- a stolen session cookie alone must not be enough to
-- permanently take over an account -- which necessarily means a wrong
-- guess and a right guess get distinguishably different responses.
-- That is a guessing oracle against the real password unless rate
-- limited, exactly the same problem login_attempts (0010) solves for
-- POST /auth/login. This table is that same defense for this endpoint.
--
-- Keyed by user_id, not email: unlike request_otp()/login(), the
-- caller here is already resolved to one specific account via their
-- session -- there is no attacker-supplied address to canonicalize or
-- plus-fold (RT-4's fix, _rate_limit_email_key) the way the other two
-- rate limiters need.
CREATE TABLE password_change_attempts (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    ip_address    TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Intended query (mirrors idx_login_attempts_email_created's shape):
--   SELECT count(*) FROM password_change_attempts WHERE user_id = ? AND created_at > now() - interval '15 minutes';
CREATE INDEX idx_password_change_attempts_user_created ON password_change_attempts (user_id, created_at);
-- Intended query -- the IP half of the same rate limit:
--   SELECT count(*) FROM password_change_attempts WHERE ip_address = ? AND created_at > now() - interval '15 minutes';
CREATE INDEX idx_password_change_attempts_ip_created ON password_change_attempts (ip_address, created_at);
