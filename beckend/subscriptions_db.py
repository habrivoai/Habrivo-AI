
from database import DatabaseConfig, connect


def init_subscriptions_db(config=None):
    config = config or DatabaseConfig.from_env()
    if config.backend != "sqlite":
        raise RuntimeError("Use the explicit PostgreSQL migration command")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = connect(config)
    try:
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id INTEGER NOT NULL,
                    plan TEXT NOT NULL
                        CHECK(plan IN ('starter', 'pro', 'premium')),
                    billing_cycle TEXT NOT NULL
                        CHECK(billing_cycle IN (
                            'monthly', 'quarterly',
                            'half_yearly', 'yearly'
                        )),
                    currency TEXT NOT NULL DEFAULT 'USD',
                    amount_usd INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    payment_reference TEXT,
                    start_date TEXT,
                    expiry_date TEXT,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
            """)
            columns = [
                row[1]
                for row in db.execute("PRAGMA table_info(subscriptions)")
            ]

            if "payment_verified" not in columns:
                db.execute(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN payment_verified INTEGER NOT NULL DEFAULT 0"
                )

            db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_subscriptions_customer
                ON subscriptions(customer_id)
            """)


            # Track successful AI promotion generations
            db.execute("""
                CREATE TABLE IF NOT EXISTS promo_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id INTEGER NOT NULL,
                    subscription_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_promo_usage_customer_date
                ON promo_usage(customer_id, created_at)
            """)

            db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    init_subscriptions_db()
    print("Subscription database initialized successfully!")
