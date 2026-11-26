CREATE TABLE user_feedback (
  id BIGSERIAL PRIMARY KEY,
  category VARCHAR(32) NOT NULL CHECK(category IN ('bug','suggestion','acceptance')),
  message VARCHAR(2000) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  github_issue_number INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  synced_at TIMESTAMPTZ
);
CREATE INDEX ix_user_feedback_pending ON user_feedback(status, id);
