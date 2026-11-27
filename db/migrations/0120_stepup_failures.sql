-- #976 slice A follow-up: wrong step-up codes are counted PER SESSION (not on the account-wide sign-in streak), so a stolen cookie can end
-- its own session by guessing but cannot lock the owner out of sign-in. 10 wrong codes in a session end that session.
ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS stepup_failed INTEGER NOT NULL DEFAULT 0;
