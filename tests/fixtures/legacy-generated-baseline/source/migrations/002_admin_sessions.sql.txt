-- Create server-side administrator sessions.
CREATE TABLE admin_sessions (
  id BIGSERIAL PRIMARY KEY,
  token_hash VARCHAR(64) NOT NULL UNIQUE,
  csrf_hash VARCHAR(64) NOT NULL,
  username VARCHAR(128) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_admin_sessions_expires ON admin_sessions(expires_at);
