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

import logging
import os
import threading
from datetime import datetime, timezone
from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify, Response

from claude_routine import fire_routine
from db import get_db, init_db
from graph_subscription import create_subscription, ensure_subscription_fresh, get_subscription_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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

# ---------------------------------------------------------------------
# Configuration - these come from Environment Variables you set in Render
# (Render dashboard > your service > Environment tab)
# ---------------------------------------------------------------------
SYNC_USERNAME = os.environ.get("SYNC_USERNAME", "changeme")
SYNC_PASSWORD = os.environ.get("SYNC_PASSWORD", "changeme")
GRAPH_CLIENT_STATE = os.environ.get("GRAPH_CLIENT_STATE", "")


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
    """
    conn = get_db()
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


# ---------------------------------------------------------------------
# Real-time carrier-email trigger (Microsoft Graph change notifications)
#
# Microsoft Graph calls /ms-graph/webhook the moment a new email lands in
# loads@monstertrucking.com's Inbox. This route hands the specific message
# off to the "Carrier Auto-Respond" Claude Code routine right away, instead
# of that routine waiting for its next hourly scheduled run.
# ---------------------------------------------------------------------
@app.route("/ms-graph/webhook", methods=["POST"])
def ms_graph_webhook():
    """
    Two very different kinds of calls land here:

    1. The one-time validation handshake, sent the moment a subscription is
       created (or its notificationUrl changes): Graph sends a
       validationToken query parameter and expects it echoed back as plain
       text within 10 seconds, or subscription creation fails.
    2. Real notifications, whenever mail actually arrives: a JSON body with
       a "value" list of one or more notification objects.

    Graph expects a fast response (a few seconds) and will retry - creating
    duplicate work - if this takes too long. So this route only validates
    and acknowledges; the actual "check Aljex and draft a reply" work is
    kicked off on a background thread after responding.
    """
    validation_token = request.args.get("validationToken")
    if validation_token is not None:
        return Response(validation_token, mimetype="text/plain", status=200)

    body = request.get_json(force=True, silent=True) or {}
    notifications = body.get("value", [])

    message_ids = []
    for note in notifications:
        if note.get("clientState") != GRAPH_CLIENT_STATE:
            app.logger.warning(
                "Ignoring a Graph notification with a mismatched clientState "
                "(subscriptionId=%s) - possibly not really from Graph.",
                note.get("subscriptionId"),
            )
            continue
        msg_id = (note.get("resourceData") or {}).get("id")
        if msg_id:
            message_ids.append(msg_id)

    if message_ids:
        threading.Thread(target=_fire_routine_for_messages, args=(message_ids,), daemon=True).start()

    return "", 202


def _fire_routine_for_messages(message_ids):
    for msg_id in message_ids:
        fire_routine(msg_id)


@app.route("/ms-graph/subscribe", methods=["POST"])
@requires_auth
def ms_graph_subscribe():
    """
    One-time (or occasional) manual bootstrap: creates a fresh Graph
    subscription right now, rather than waiting for the daily background
    check to notice none exists yet. Call this once, right after this
    feature is first deployed. Protected by the same username/password as
    this app's other admin routes.
    """
    try:
        result = create_subscription()
        return jsonify({"status": "ok", "subscription": result})
    except Exception as exc:
        app.logger.exception("Manual /ms-graph/subscribe call failed.")
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/health/graph-subscription", methods=["GET"])
@requires_auth
def graph_subscription_health():
    """Current Graph subscription status, for checking this isn't quietly broken."""
    state = get_subscription_state()
    if not state:
        return jsonify({
            "status": "no_subscription",
            "message": "No subscription has been created yet. POST /ms-graph/subscribe to create one.",
        }), 200
    return jsonify(state)


def _start_scheduler():
    """
    Runs ensure_subscription_fresh() once at startup and then once a day,
    so the Graph subscription renews itself well before its ~3-day
    expiration without needing a separate scheduled job elsewhere.
    """
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        ensure_subscription_fresh,
        "interval",
        hours=24,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    return scheduler


init_db()
_scheduler = _start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
