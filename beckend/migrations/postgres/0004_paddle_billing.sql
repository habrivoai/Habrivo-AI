-- Additive Paddle linkage and receipts only; no existing customer/subscription rewrite.
CREATE TABLE paddle_checkout_links (
    checkout_ref TEXT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES users(id),
    plan TEXT NOT NULL CHECK (plan IN ('starter', 'pro', 'premium')),
    billing_cycle TEXT NOT NULL CHECK (billing_cycle IN ('monthly', 'quarterly', 'half_yearly', 'yearly')),
    price_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    amount_usd BIGINT NOT NULL CHECK (amount_usd > 0),
    environment TEXT NOT NULL CHECK (environment IN ('sandbox', 'live')),
    transaction_id TEXT UNIQUE
);
CREATE TABLE paddle_subscription_state (
    paddle_subscription_id TEXT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES users(id),
    paddle_customer_id TEXT NOT NULL,
    initial_transaction_id TEXT NOT NULL UNIQUE,
    checkout_ref TEXT NOT NULL UNIQUE REFERENCES paddle_checkout_links(checkout_ref),
    local_subscription_id BIGINT UNIQUE REFERENCES subscriptions(id),
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('pending','active','trialing','past_due','paused','canceled')),
    lifecycle_at TEXT,
    initial_start TEXT,
    initial_end TEXT
);
CREATE INDEX idx_paddle_subscription_customer ON paddle_subscription_state(paddle_customer_id, customer_id);
CREATE TABLE paddle_payments (
    transaction_id TEXT PRIMARY KEY,
    paddle_subscription_id TEXT NOT NULL REFERENCES paddle_subscription_state(paddle_subscription_id),
    starts_at TEXT,
    ends_at TEXT,
    proof_checksum TEXT NOT NULL,
    CHECK ((starts_at IS NULL AND ends_at IS NULL) OR
           (starts_at IS NOT NULL AND ends_at IS NOT NULL AND starts_at < ends_at))
);
CREATE INDEX idx_paddle_payments_period ON paddle_payments(paddle_subscription_id, ends_at);
CREATE TABLE paddle_webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_checksum TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('processed', 'ignored'))
);
