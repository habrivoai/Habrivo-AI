-- Additive customer authentication state; preserve user IDs and password hashes.
CREATE TABLE customer_sessions (
    token_hash TEXT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES users(id),
    credential_version TEXT NOT NULL,
    expires_at BIGINT NOT NULL
);
CREATE INDEX idx_customer_sessions_owner ON customer_sessions(customer_id);
CREATE INDEX idx_customer_sessions_expiry ON customer_sessions(expires_at);
CREATE TABLE password_reset_tokens (
    token_hash TEXT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES users(id),
    credential_version TEXT NOT NULL,
    expires_at BIGINT NOT NULL
);
CREATE INDEX idx_password_reset_tokens_owner ON password_reset_tokens(customer_id);
CREATE TABLE password_reset_requests (
    request_id TEXT PRIMARY KEY,
    customer_id BIGINT REFERENCES users(id),
    credential_version TEXT,
    queued_at BIGINT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','sending','sent','failed','expired'))
);
CREATE INDEX idx_password_reset_requests_queue ON password_reset_requests(state, queued_at);
