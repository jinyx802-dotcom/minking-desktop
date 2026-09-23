#!/usr/bin/env python3
"""Copy accounts, API keys, routes, and admin credentials from SQLite into MySQL.

Does not copy call records, usage, error ring, or session/response bindings.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

TABLES = (
    "accounts",
    "api_keys",
    "api_key_routes",
    "admin_credentials",
)


def rows_from_sqlite(path: Path, table: str) -> list[dict]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in names:
            return []
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        connection.close()


def mysql_columns(cursor, table: str) -> list[str]:
    cursor.execute(f"SHOW COLUMNS FROM {table}")
    return [str(row["Field"]) for row in cursor.fetchall()]


def upsert(cursor, table: str, row: dict, columns: list[str]) -> None:
    present = [column for column in columns if column in row]
    placeholders = ",".join(["%s"] * len(present))
    assignments = ",".join(f"{column}=VALUES({column})" for column in present)
    sql = (
        f"INSERT INTO {table} ({','.join(present)}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {assignments}"
    )
    cursor.execute(sql, tuple(row[column] for column in present))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    if not args.sqlite.is_file():
        print(f"sqlite not found: {args.sqlite}", file=sys.stderr)
        return 1
    mysql = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )
    try:
        with mysql.cursor() as cursor:
            mysql.query("SET FOREIGN_KEY_CHECKS=0")
            for table in TABLES:
                items = rows_from_sqlite(args.sqlite, table)
                columns = mysql_columns(cursor, table)
                for item in items:
                    upsert(cursor, table, item, columns)
                print(f"{table} {len(items)}")
            mysql.query("SET FOREIGN_KEY_CHECKS=1")
        mysql.commit()
    except Exception:
        mysql.rollback()
        raise
    finally:
        mysql.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
