-- 0008_otp_send_status.sql
--
-- Adds delivery-outcome tracking to otp_codes (0005_auth.sql). Needed
-- for AUTH-OTP's DoD #5: "Quota exhaustion must fail VISIBLY -- logged,
-- and surfaced to admins -- never a silent non-delivery that looks like
-- a working login to the user." Nothing in 0005 records whether the
-- Resend call for a given code actually succeeded; without that column,
-- a silent Resend outage or a hit against the 100/day free-tier ceiling
-- (ADR §9.3's capacity note) would leave `otp_codes` looking identical
-- to a healthy day -- there would be nothing in the database an admin
-- endpoint could query to surface the problem.
--
-- `send_status` starts 'pending' at insert (the moment AUTH-OTP has
-- generated and hashed a code but not yet called Resend) and the
-- service updates it to exactly one of:
--   'sent'           -- Resend accepted the request.
--   'failed'         -- Resend/network error, not quota-related.
--   'quota_exceeded' -- the daily-cap pre-check tripped before a send
--                       was even attempted, or Resend itself returned
--                       429/quota-exceeded.
-- `send_error` carries a short, non-sensitive diagnostic string (the
-- provider's own error text) for that admin-facing view -- never the
-- OTP code, which was never stored anywhere but `code_hash` to begin
-- with.
--
-- This migration does not touch the code/expiry/rate-limit columns or
-- indexes 0005 already built (email/IP rate limiting reads those
-- unchanged) -- it only adds the delivery-outcome fields AUTH-OTP's
-- send path needs, plus one partial index, mirroring 0005's own
-- "index only the interesting subset" pattern
-- (idx_otp_codes_email_active), sized for exactly the query AUTH-OTP's
-- admin-only `GET /auth/otp-failures` endpoint runs:
--   SELECT * FROM otp_codes WHERE send_status <> 'sent' ORDER BY created_at DESC;

ALTER TABLE otp_codes
    ADD COLUMN send_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (send_status IN ('pending', 'sent', 'failed', 'quota_exceeded')),
    ADD COLUMN send_error TEXT NOT NULL DEFAULT '';

CREATE INDEX idx_otp_codes_send_status_failures
    ON otp_codes (created_at DESC)
    WHERE send_status <> 'sent';
