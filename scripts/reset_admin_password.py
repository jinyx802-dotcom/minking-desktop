from __future__ import annotations

import argparse
import getpass
import sqlite3
from pathlib import Path

from argon2 import PasswordHasher, Type

from app.admin_auth import ADMIN_USERNAME, MIN_PASSWORD_LENGTH
from app.config import settings
from app.store.gateway import iso_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactively reset the Transfer Station admin password."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=settings.gateway_db_path(),
        help="SQLite database path (default: DATA_DIR/gateway.sqlite3)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"Database does not exist: {database}")
    password = getpass.getpass("New admin password: ")
    confirmation = getpass.getpass("Confirm new admin password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    if len(password) < MIN_PASSWORD_LENGTH or password.isspace():
        raise SystemExit(f"Password must contain at least {MIN_PASSWORD_LENGTH} characters")
    hasher = PasswordHasher(
        time_cost=3,
        memory_cost=64 * 1024,
        parallelism=2,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )
    password_hash = hasher.hash(password)
    connection = sqlite3.connect(database, timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        cursor = connection.execute(
            """
            UPDATE admin_credentials SET password_hash=?,password_updated_at=?
            WHERE username=?
            """,
            (password_hash, iso_now(), ADMIN_USERNAME),
        )
        if cursor.rowcount != 1:
            raise SystemExit("Administrator credential is not initialized")
        connection.execute("DELETE FROM admin_sessions")
        connection.commit()
    finally:
        connection.close()
    print("Admin password reset. All existing admin sessions were revoked.")


if __name__ == "__main__":
    main()
