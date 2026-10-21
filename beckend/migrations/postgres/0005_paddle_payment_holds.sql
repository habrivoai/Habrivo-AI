-- Conservative reconciliation holds, not assertions that refunds are final.
-- No FK to paddle_payments: a linked adjustment can arrive before paid proof.
CREATE TABLE paddle_payment_holds (
    transaction_id TEXT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES users(id),
    paddle_customer_id TEXT,
    first_event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason = 'adjustment_requires_reconciliation')
);
