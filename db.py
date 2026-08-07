"""
db.py
-----
Real persistent storage to replace the Claude-artifact-only
`window.storage` API. Same two-scope idea (shared = visible to
everyone, personal = scoped to one visitor), just backed by SQLite
instead of the artifact runtime.

'Personal' scope is identified by a random id stored in each visitor's
signed Flask session cookie (see app.py) -- there's no real login
system, so this is "this browser," not "this person." Good enough for
a school project demo; a real deployment would want actual accounts.
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "fixitflow.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (scope, key)
        )
    """)
    conn.commit()
    conn.close()


def kv_get(scope: str, key: str):
    conn = get_db()
    row = conn.execute("SELECT value FROM kv WHERE scope = ? AND key = ?", (scope, key)).fetchone()
    conn.close()
    return row["value"] if row else None


def kv_set(scope: str, key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO kv (scope, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value",
        (scope, key, value),
    )
    conn.commit()
    conn.close()
