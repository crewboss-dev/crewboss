-- #976 slice D: a LAN install's runners pin the server's self-signed certificate. When the owner prepares a new one, the server sends each
-- connected runner a signed `tls_rotate` frame; a runner that verified it answers with the new certificate's pin, which is kept here:
--   tls_fp      the 26-character pin (lower-case base32 of sha256(SPKI)) of the newest certificate this device acknowledged
--   tls_fp_at   when it acknowledged
-- NULL = never acknowledged one (a runner enrolled before LAN mode, or against a public address: those need no rotation).
-- 0118 is the sign-in slice's (mfa_default); this is the next free number after it.
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS tls_fp TEXT;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS tls_fp_at TIMESTAMPTZ;
