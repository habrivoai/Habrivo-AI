-- No backfill: historical accounts are not implicitly eligible for a trial.
CREATE TABLE email_verification_accounts (
    customer_id BIGINT PRIMARY KEY REFERENCES users(id),
    eligible BIGINT NOT NULL CHECK (eligible IN (0,1)),
    email_hash TEXT NOT NULL,
    verified_at BIGINT
);
CREATE TABLE email_verification_tokens (
    token_hash TEXT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES email_verification_accounts(customer_id),
    email_hash TEXT NOT NULL,
    expires_at BIGINT NOT NULL
);
CREATE INDEX idx_email_verification_tokens_owner ON email_verification_tokens(customer_id);
CREATE TABLE email_verification_requests (
    request_id TEXT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES email_verification_accounts(customer_id),
    email_hash TEXT NOT NULL,
    queued_at BIGINT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','sending','sent','failed','expired'))
);
CREATE INDEX idx_email_verification_requests_queue ON email_verification_requests(state,queued_at);
CREATE TABLE marketing_trial_grants (
    customer_id BIGINT PRIMARY KEY REFERENCES email_verification_accounts(customer_id),
    granted_at BIGINT NOT NULL,
    allowance BIGINT NOT NULL DEFAULT 3 CHECK (allowance = 3),
    used BIGINT NOT NULL DEFAULT 0 CHECK (used >= 0 AND used <= 3)
);
CREATE TABLE marketing_trial_usage (
    attempt_id TEXT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES marketing_trial_grants(customer_id),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_marketing_trial_usage_customer_date ON marketing_trial_usage(customer_id,created_at);
