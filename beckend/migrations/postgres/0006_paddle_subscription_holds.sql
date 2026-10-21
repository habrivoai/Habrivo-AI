-- Monotonic subscription reconciliation; no automatic release or plan conversion.
CREATE TABLE paddle_subscription_holds (
    paddle_subscription_id TEXT PRIMARY KEY REFERENCES paddle_subscription_state(paddle_subscription_id),
    customer_id BIGINT NOT NULL REFERENCES users(id),
    first_event_id TEXT NOT NULL,
    first_payload_checksum TEXT NOT NULL,
    watermark TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason = 'unsupported_catalog_requires_reconciliation')
);
