"""Additive console migrations. No historical charges are recomputed."""
from app.store.gateway import gateway_store


async def migrate_console() -> None:
    def migrate(db):
        mysql = gateway_store.engine == "mysql"
        text = "VARCHAR(191)" if mysql else "TEXT"
        amount = "DECIMAL(24,8)" if mysql else "TEXT"
        for table, column, ddl in (
            ("billing_settings", "request_budget_usd", f"{amount} NOT NULL DEFAULT '0.50'"),
            ("portal_users", "request_budget_usd", f"{amount} NULL"),
            ("video_jobs", "billing_request_id", f"{text} NULL"),
            ("api_keys", "usd_credit", f"{amount} NOT NULL DEFAULT '0.00'"),
            ("wallet_ledger", "key_id", f"{text} NULL"),
            ("model_official_prices", "price_source", f"{text} NOT NULL DEFAULT 'catalog'"),
        ):
            if mysql:
                present = db.execute(f"SHOW COLUMNS FROM {table} LIKE '{column}'").fetchone()
            else:
                present = any(r["name"] == column for r in db.execute(f"PRAGMA table_info({table})").fetchall())
            if not present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        if mysql:
            for table, columns in {
                "portal_users": ["usd_credit"],
                "wallet_ledger": ["amount_usd", "balance_after", "official_usd"],
                "call_records": ["usd_charged"],
            }.items():
                for column in columns:
                    info = db.execute(f"SHOW COLUMNS FROM {table} LIKE '{column}'").fetchone()
                    if str(info.get("Type", "")).lower() != "decimal(24,8)":
                        db.execute(f"ALTER TABLE {table} MODIFY COLUMN {column} DECIMAL(24,8) " + ("NULL" if column in {"official_usd", "usd_charged"} else "NOT NULL DEFAULT '0'"))
        db.execute(f"""CREATE TABLE IF NOT EXISTS billing_requests (
            request_id {text} PRIMARY KEY, user_id {text} NOT NULL, key_id {text} NULL,
            model {text} NOT NULL, endpoint {text} NOT NULL,
            state {text} NOT NULL, reserved_usd {amount} NOT NULL,
            actual_usd {amount} NULL, charged_usd {amount} NOT NULL DEFAULT '0',
            absorbed_usd {amount} NOT NULL DEFAULT '0', price_version {text} NOT NULL,
            price_snapshot TEXT NOT NULL, settings_snapshot TEXT NOT NULL,
            usage_snapshot TEXT NULL, outcome {text} NULL,
            created_at {text} NOT NULL, expires_at {text} NOT NULL, updated_at {text} NOT NULL
        )""")
        if mysql:
            bill_key = db.execute("SHOW COLUMNS FROM billing_requests LIKE 'key_id'").fetchone()
        else:
            bill_key = any(row["name"] == "key_id" for row in db.execute("PRAGMA table_info(billing_requests)").fetchall())
        if not bill_key:
            db.execute(f"ALTER TABLE billing_requests ADD COLUMN key_id {text} NULL")
        db.execute(f"""CREATE TABLE IF NOT EXISTS routing_leases (
            lease_id {text} PRIMARY KEY, account_id {text} NOT NULL,
            provider {text} NOT NULL, model {text} NOT NULL, work_units DOUBLE NOT NULL,
            expires_at {text} NOT NULL
        )""")
        db.execute(f"""CREATE TABLE IF NOT EXISTS routing_bindings (
            binding_hash {text} PRIMARY KEY, account_id {text} NOT NULL, expires_at {text} NOT NULL
        )""")
        db.execute(f"""CREATE TABLE IF NOT EXISTS routing_events (
            id {text} PRIMARY KEY, key_id {text} NOT NULL, account_id {text} NOT NULL,
            provider {text} NOT NULL, reason {text} NOT NULL, created_at {text} NOT NULL
        )""")
        db.execute(f"CREATE TABLE IF NOT EXISTS console_migrations (name {text} PRIMARY KEY, applied_at {text} NOT NULL)")
        db.execute(f"CREATE TABLE IF NOT EXISTS routing_legacy (key_id {text} NOT NULL,provider {text} NOT NULL,account_id {text} NOT NULL,expires_at {text} NOT NULL,PRIMARY KEY(key_id,provider))")
        if mysql:
            # Existing tables may use the server default while the database has
            # another default. Joined identifiers must use the same collation.
            import re
            info = db.execute("SELECT COLLATION_NAME AS name FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_records' AND COLUMN_NAME='request_id'").fetchone()
            collation = info['name']
            if not re.fullmatch(r'utf8mb4_[a-zA-Z0-9_]+',collation):
                raise ValueError('Unsupported request identifier collation')
            for table in ('billing_requests','routing_leases','routing_bindings','routing_events','routing_legacy','console_migrations'):
                current = db.execute("SELECT TABLE_COLLATION AS name FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?", (table,)).fetchone()
                if current['name'] != collation:
                    db.execute(f"ALTER TABLE {table} CONVERT TO CHARACTER SET utf8mb4 COLLATE {collation}")
        if mysql:
            user_column = db.execute("SHOW COLUMNS FROM wallet_ledger LIKE 'user_id'").fetchone()
            if user_column and str(user_column.get("Null") or "") == "NO":
                db.execute("ALTER TABLE wallet_ledger MODIFY COLUMN user_id VARCHAR(64) NULL")
        else:
            ledger_columns = list(db.execute("PRAGMA table_info(wallet_ledger)").fetchall())
            user_column = next(row for row in ledger_columns if row["name"] == "user_id")
            if int(user_column["notnull"]):
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute(
                    """
                    CREATE TABLE wallet_ledger_new (
                        id TEXT PRIMARY KEY,
                        user_id TEXT,
                        key_id TEXT,
                        amount_usd TEXT NOT NULL,
                        balance_after TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        request_id TEXT,
                        card_id TEXT,
                        actor TEXT,
                        reason TEXT,
                        official_usd TEXT,
                        multiplier TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO wallet_ledger_new(
                        id,user_id,key_id,amount_usd,balance_after,kind,request_id,card_id,
                        actor,reason,official_usd,multiplier,created_at
                    )
                    SELECT id,user_id,key_id,amount_usd,balance_after,kind,request_id,card_id,
                           actor,reason,official_usd,multiplier,created_at
                    FROM wallet_ledger
                    """
                )
                db.execute("DROP TABLE wallet_ledger")
                db.execute("ALTER TABLE wallet_ledger_new RENAME TO wallet_ledger")
                db.execute("CREATE INDEX IF NOT EXISTS ix_wallet_ledger_user ON wallet_ledger(user_id, created_at DESC)")
                db.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_ledger_usage_request
                    ON wallet_ledger(request_id) WHERE kind='usage' AND request_id IS NOT NULL
                    """
                )
                db.execute("PRAGMA foreign_keys=ON")
        if not db.execute("SELECT name FROM console_migrations WHERE name='balances_on_keys'").fetchone():
            from app.billing import usd_text
            from app.store.gateway import iso_now
            for user in db.execute("SELECT id, usd_credit FROM portal_users").fetchall():
                key = db.execute(
                    "SELECT id FROM api_keys WHERE owner_user_id=? AND status!='deleted' ORDER BY created_at LIMIT 1",
                    (user["id"],),
                ).fetchone()
                if key:
                    db.execute(
                        "UPDATE api_keys SET usd_credit=? WHERE id=?",
                        (usd_text(user["usd_credit"]), key["id"]),
                    )
            db.execute(
                "INSERT INTO console_migrations(name,applied_at) VALUES('balances_on_keys',?)",
                (iso_now(),),
            )
        if not db.execute("SELECT name FROM console_migrations WHERE name='routing_v1'").fetchone():
            from app.scheduler import expiry
            from app.store.gateway import iso_now
            for row in db.execute("SELECT key_id,provider,active_account_id FROM api_key_routes WHERE active_account_id IS NOT NULL").fetchall():
                db.execute("INSERT INTO routing_legacy(key_id,provider,account_id,expires_at) VALUES(?,?,?,?)", (row["key_id"],row["provider"],row["active_account_id"],expiry(7*24*60)))
            db.execute("INSERT INTO console_migrations(name,applied_at) VALUES('routing_v1',?)", (iso_now(),))
        for table,name,columns in (
            ('billing_requests','ix_billing_user_state','user_id,state,created_at'),
            ('billing_requests','ix_billing_expiry','state,expires_at'),
            ('routing_leases','ix_routing_lease_account','account_id,expires_at'),
            ('routing_events','ix_routing_event_provider','provider,created_at'),
            ('routing_events','ix_routing_event_account','account_id,created_at'),
        ):
            if mysql:
                present = db.execute(f"SHOW INDEX FROM {table} WHERE Key_name='{name}'").fetchone()
                if not present:
                    db.execute(f"CREATE INDEX {name} ON {table}({columns})")
            else:
                db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")
    await gateway_store.call(migrate)
