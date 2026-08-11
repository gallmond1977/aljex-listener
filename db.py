"""
Shared SQLite setup for this app.

Pulled out of app.py so other modules (like graph_subscription.py) can read
and write the database without creating an import cycle with app.py.
"""

import os
import sqlite3

DATABASE_PATH = os.environ.get("DATABASE_PATH", "aljex_data.db")


def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aljex_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            record_id TEXT NOT NULL,
            action TEXT NOT NULL,
            data_json TEXT NOT NULL,
            received_at TEXT NOT NULL,
            UNIQUE(table_name, record_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rep_notes (
            customer_id TEXT PRIMARY KEY,
            tier TEXT,
            last_touched TEXT,
            next_touch_date TEXT,
            next_action TEXT,
            notes TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_subscription (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            subscription_id TEXT,
            expiration_datetime TEXT,
            last_checked_at TEXT,
            last_status TEXT,
            last_error TEXT
        )
        """
    )
    conn.commit()
    conn.close()
