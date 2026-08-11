"""
Aljex Live Sync Listener
-------------------------
This small web app receives real-time data updates from Aljex (via their
Live Sync feature) and saves them into a simple database so other tools
(like a CRM or dashboard) can read from it later.

How it works:
- Aljex sends an HTTP POST request every time a record is created,
  updated, or deleted in Aljex.
- This app checks that the request is actually from Aljex (using a
  username/password you set), reads the data, and saves it.
- You can see a simple status page at "/" to confirm it's running.
"""

import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, Response

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    # Allows a dashboard webpage (on a different address) to read data
    # from this listener. Only GET/POST requests to /records/... and
    # /notes/... are affected; this does not weaken password protection.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/records/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_records(_subpath):
    return "", 204


@app.route("/notes", methods=["OPTIONS"])
@app.route("/notes/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_notes(_subpath=None):
    return "", 204


@app.route("/aljex-webhook", methods=["OPTIONS"])
def cors_preflight_webhook():
    return "", 204


@app.route("/bulk-import/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_bulk_import(_subpath):
    return "", 204

# ---------------------------------------------------------------------
# Configuration - these come from Environment Variables you set in Render
# (Render dashboard > your service > Environment tab)
# ---------------------------------------------------------------------
SYNC_USERNAME = os.environ.get("SYNC_USERNAME", "changeme")
SYNC_PASSWORD = os.environ.get("SYNC_PASSWORD", "changeme")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "aljex_data.db")


# ---------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------
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
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Simple username/password protection
# (Basic Auth - this matches the "username and password" Aljex asks for)
# ---------------------------------------------------------------------
def check_auth(username, password):
    return username == SYNC_USERNAME and password == SYNC_PASSWORD


def authenticate():
    return Response(
        "Login required.", 401, {"WWW-Authenticate": 'Basic realm="Login Required"'}
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/", methods=["GET"])
def status():
    """Simple status page so you can confirm the listener is alive."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM aljex_records").fetchone()["c"]
    conn.close()
    return jsonify(
        {
            "status": "running",
            "records_stored": count,
            "message": "Aljex Live Sync listener is up and waiting for data.",
        }
    )


@app.route("/aljex-webhook", methods=["POST"])
@requires_auth
def aljex_webhook():
    """
    This is the address you give to Aljex for Live Sync.
    It receives one record at a time as form data, e.g.:
        web_sync_table_name=loads
        web_sync_action=update
        id=12345
        ...other fields...
    """
    form = request.form.to_dict()

    table_name = form.pop("web_sync_table_name", None)
    action = form.pop("web_sync_action", None)

    if not table_name or not action:
        return jsonify({"error": "Missing required sync parameters"}), 400

    record_id = form.get("id", "")

    import json

    conn = get_db()

    if action == "delete":
        conn.execute(
            "DELETE FROM aljex_records WHERE table_name = ? AND record_id = ?",
            (table_name, record_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO aljex_records (table_name, record_id, action, data_json, received_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(table_name, record_id) DO UPDATE SET
                action = excluded.action,
                data_json = excluded.data_json,
                received_at = excluded.received_at
            """,
            (
                table_name,
                record_id,
                action,
                json.dumps(form),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "table": table_name, "action": action}), 200


@app.route("/records/<table_name>", methods=["GET"])
@requires_auth
def view_records(table_name):
    """
    A simple way to peek at what's been stored for a given table,
    e.g. /records/loads or /records/customers

    By default returns the most recent 100 records. Add ?limit=all
    to the URL to get every record for that table instead (used by
    the rep view, since it needs the full customer/salesrep list,
    not just the most recent ones).
    """
    limit_param = request.args.get("limit", "100")

    conn = get_db()
    if limit_param == "all":
        rows = conn.execute(
            "SELECT record_id, action, data_json, received_at FROM aljex_records WHERE table_name = ? ORDER BY received_at DESC",
            (table_name,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT record_id, action, data_json, received_at FROM aljex_records WHERE table_name = ? ORDER BY received_at DESC LIMIT 100",
            (table_name,),
        ).fetchall()
    conn.close()

    import json

    results = [
        {
            "record_id": r["record_id"],
            "action": r["action"],
            "data": json.loads(r["data_json"]),
            "received_at": r["received_at"],
        }
        for r in rows
    ]
    return jsonify(results)


@app.route("/records/<table_name>/<record_id>", methods=["GET"])
@requires_auth
def get_record_by_id(table_name, record_id):
    """
    Look up a single record by its ID, e.g. /records/loads/12345
    This is faster and more reliable than scanning the latest 100 records.
    """
    import json

    conn = get_db()
    row = conn.execute(
        """
        SELECT table_name, record_id, action, data_json, received_at
        FROM aljex_records
        WHERE table_name = ? AND record_id = ?
        """,
        (table_name, record_id),
    ).fetchone()
    conn.close()

    if row is None:
        return jsonify({"error": "record not found"}), 404

    return jsonify(
        {
            "table_name": row["table_name"],
            "record_id": row["record_id"],
            "action": row["action"],
            "data": json.loads(row["data_json"]),
            "received_at": row["received_at"],
        }
    )


@app.route("/notes", methods=["GET"])
@requires_auth
def get_all_notes():
    """Returns all saved rep notes (tier, last touched, next action, etc.)"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM rep_notes").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/notes/<customer_id>", methods=["POST"])
@requires_auth
def save_note(customer_id):
    """
    Saves or updates the rep-entered fields for one customer.
    Expects JSON body, e.g.:
        {"tier": "1", "last_touched": "2026-08-06", "next_touch_date": "2026-08-13",
         "next_action": "Call", "notes": "Talked to Will, same office"}
    """
    body = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    conn.execute(
        """
        INSERT INTO rep_notes (customer_id, tier, last_touched, next_touch_date, next_action, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            tier = excluded.tier,
            last_touched = excluded.last_touched,
            next_touch_date = excluded.next_touch_date,
            next_action = excluded.next_action,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            customer_id,
            body.get("tier", ""),
            body.get("last_touched", ""),
            body.get("next_touch_date", ""),
            body.get("next_action", ""),
            body.get("notes", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "customer_id": customer_id})


@app.route("/bulk-import/<table_name>", methods=["POST"])
@requires_auth
def bulk_import(table_name):
    """
    Accepts a JSON array of records and saves them all at once, safely.
    Used for one-time backfills (e.g. importing a CSV export) instead of
    sending thousands of individual webhook-style requests.

    Expects JSON body: a list of objects, each with at least an "id" field.
    """
    import json

    records = request.get_json(force=True, silent=True)
    if not isinstance(records, list):
        return jsonify({"error": "Expected a JSON array of records"}), 400

    conn = get_db()
    saved = 0
    skipped = 0

    for record in records:
        record_id = record.get("id", "")
        if not record_id:
            skipped += 1
            continue
        conn.execute(
            """
            INSERT INTO aljex_records (table_name, record_id, action, data_json, received_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(table_name, record_id) DO UPDATE SET
                action = excluded.action,
                data_json = excluded.data_json,
                received_at = excluded.received_at
            """,
            (
                table_name,
                record_id,
                "update",
                json.dumps(record),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        saved += 1

    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "table": table_name, "saved": saved, "skipped": skipped})


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
