-- Shared auth limits only. No trials, subscriptions, verification grants or user rewrites.
CREATE TABLE account_action_limits (
    scope TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    window_start BIGINT NOT NULL,
    attempts BIGINT NOT NULL,
    PRIMARY KEY (scope, subject_hash, window_start)
);
CREATE INDEX idx_account_action_limits_window ON account_action_limits(window_start);
