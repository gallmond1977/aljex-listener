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
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify, Response

from claude_routine import fire_routine
from graph_auth import get_graph_token
from graph_subscription import TARGET_MAILBOX, create_subscription, ensure_subscription_fresh, get_subscription_state

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
GRAPH_CLIENT_STATE = os.environ.get("GRAPH_CLIENT_STATE", "")


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
            commission_expiration TEXT,
            updated_at TEXT
        )
        """
    )

    # Safety net: if rep_notes already existed from before commission_expiration
    # was added, the CREATE TABLE above won't retroactively add the column.
    # This adds it if missing, without touching any existing data.
    existing_columns = [row["name"] for row in conn.execute("PRAGMA table_info(rep_notes)").fetchall()]
    if "commission_expiration" not in existing_columns:
        conn.execute("ALTER TABLE rep_notes ADD COLUMN commission_expiration TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads_status (
            lead_key TEXT PRIMARY KEY,
            status TEXT,
            marked_by TEXT,
            notes TEXT,
            next_followup TEXT,
            updated_at TEXT
        )
        """
    )

    # Safety net: same pattern as commission_expiration above — add any
    # columns if leads_status already existed without them.
    leads_columns = [row["name"] for row in conn.execute("PRAGMA table_info(leads_status)").fetchall()]
    if "notes" not in leads_columns:
        conn.execute("ALTER TABLE leads_status ADD COLUMN notes TEXT")
    if "next_followup" not in leads_columns:
        conn.execute("ALTER TABLE leads_status ADD COLUMN next_followup TEXT")
    if "assigned_rep" not in leads_columns:
        conn.execute("ALTER TABLE leads_status ADD COLUMN assigned_rep TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deleted_leads (
            lead_key TEXT PRIMARY KEY,
            deleted_by TEXT,
            deleted_at TEXT
        )
        """
    )

    # Leads that reps type in by hand — not derived from any Aljex load or
    # customer record. Each row becomes its own lead card in the tool,
    # keyed as "MANUAL:<id>" everywhere leads_status is used.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            city TEXT,
            state TEXT,
            address TEXT,
            contact TEXT,
            phone TEXT,
            email TEXT,
            created_by TEXT,
            created_at TEXT
        )
        """
    )

    # Safety net: same pattern as the other tables above — add the column
    # if manual_leads already existed without it.
    manual_leads_columns = [row["name"] for row in conn.execute("PRAGMA table_info(manual_leads)").fetchall()]
    if "email" not in manual_leads_columns:
        conn.execute("ALTER TABLE manual_leads ADD COLUMN email TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_assignments (
            customer_id TEXT PRIMARY KEY,
            assigned_rep TEXT,
            assigned_by TEXT,
            updated_at TEXT
        )
        """
    )

    # -----------------------------------------------------------------
    # Service rep tables: separate from the sales-side tables above.
    # A customer can have a sales rep (customer_assignments / rep_notes)
    # AND an independent service rep who does the day-to-day check-in
    # calls (service_assignments / service_notes / customer_contacts).
    # -----------------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_assignments (
            customer_id TEXT PRIMARY KEY,
            service_rep TEXT,
            assigned_by TEXT,
            updated_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_notes (
            customer_id TEXT PRIMARY KEY,
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
        CREATE TABLE IF NOT EXISTS customer_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL,
            name TEXT,
            phone TEXT,
            email TEXT,
            is_primary INTEGER DEFAULT 0,
            updated_at TEXT
        )
        """
    )

    # Used by graph_subscription.py to track the Microsoft Graph change
    # notification subscription that powers the real-time carrier-email
    # trigger. graph_subscription.py reads/writes this via db.py's own
    # get_db() (a separate connection helper to the same database file,
    # kept there to avoid an import cycle with this module) - db.py's own
    # init_db() isn't what runs at startup here, so this table is created
    # here instead to make sure it actually exists.
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

    # Microsoft Graph is documented to sometimes deliver the same change
    # notification more than once. message_id is the primary key so a
    # second INSERT for the same message fails - see _mark_seen_once().
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_webhook_seen (
            message_id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL
        )
        """
    )

    # This is a live dispatch inbox - staff read/move/delete new mail within
    # seconds, well before a real-time-triggered routine run can spin up and
    # look the message up itself. So the webhook captures the message's
    # content immediately (see _fetch_and_cache_message()) and this table
    # holds that snapshot, keyed by message_id, for the routine to use
    # directly instead of racing staff to a live mailbox lookup.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_message_cache (
            message_id TEXT PRIMARY KEY,
            sender_name TEXT,
            sender_address TEXT,
            subject TEXT,
            body_content_type TEXT,
            body_content TEXT,
            received_datetime TEXT,
            is_read INTEGER,
            fetched_at TEXT NOT NULL
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
        INSERT INTO rep_notes (customer_id, tier, last_touched, next_touch_date, next_action, notes, commission_expiration, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            tier = excluded.tier,
            last_touched = excluded.last_touched,
            next_touch_date = excluded.next_touch_date,
            next_action = excluded.next_action,
            notes = excluded.notes,
            commission_expiration = excluded.commission_expiration,
            updated_at = excluded.updated_at
        """,
        (
            customer_id,
            body.get("tier", ""),
            body.get("last_touched", ""),
            body.get("next_touch_date", ""),
            body.get("next_action", ""),
            body.get("notes", ""),
            body.get("commission_expiration", ""),
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
        if not _mark_seen_once(msg_id):
            app.logger.info(
                "Ignoring duplicate Graph notification for message_id=%s "
                "(already fired for this message).",
                msg_id,
            )
            continue
        content = _fetch_and_cache_message(msg_id)
        _queue_for_coalesced_fire(msg_id, content)


# ---------------------------------------------------------------------
# Notification coalescing
#
# Graph can call /ms-graph/webhook several times in quick succession - e.g.
# a burst of separate emails arriving together, or one POST's "value" list
# already containing several notifications. Firing a brand-new Claude Code
# routine session per message would mean several concurrent sessions all
# racing to draft replies out of the same inbox. So instead of firing
# immediately, each message is queued and a short debounce timer is
# (re)armed; when the timer finally fires (i.e. no new message showed up
# for NOTIFICATION_DEBOUNCE_SECONDS), every message queued since the last
# fire goes out together as a single fire_routine() call.
#
# Message content is still captured immediately in _fetch_and_cache_message
# above, before queuing - only *firing the routine* is debounced, not the
# time-critical content capture, so this doesn't reopen the staff-race
# problem that motivated capturing content at webhook time in the first
# place.
# ---------------------------------------------------------------------
NOTIFICATION_DEBOUNCE_SECONDS = float(os.environ.get("NOTIFICATION_DEBOUNCE_SECONDS", "3"))

_pending_lock = threading.Lock()
_pending_messages = []
_pending_timer = None


def _queue_for_coalesced_fire(message_id, content):
    global _pending_timer

    with _pending_lock:
        _pending_messages.append(
            {
                "message_id": message_id,
                "content": content,
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if _pending_timer is not None:
            _pending_timer.cancel()
        _pending_timer = threading.Timer(NOTIFICATION_DEBOUNCE_SECONDS, _flush_pending_messages)
        _pending_timer.daemon = True
        _pending_timer.start()


def _flush_pending_messages():
    global _pending_timer

    with _pending_lock:
        batch = _pending_messages[:]
        _pending_messages.clear()
        _pending_timer = None

    if batch:
        fire_routine(batch)


_GRAPH_MESSAGE_SELECT = "subject,from,body,receivedDateTime,isRead"
# Must match graph_subscription.create_subscription()'s Prefer header - the
# notification's message_id is an immutable ID, so every fetch that uses it
# (the direct GET below, and both fallbacks) has to ask for immutable IDs
# too, or the id simply won't match anything Graph returns.
_IMMUTABLE_ID_HEADER = {"Prefer": 'IdType="ImmutableId"'}


def _fetch_message_by_id(message_id, token):
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{TARGET_MAILBOX}/messages/{message_id}",
        params={"$select": _GRAPH_MESSAGE_SELECT},
        headers={"Authorization": f"Bearer {token}", **_IMMUTABLE_ID_HEADER},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _find_message_in(url, params, message_id, token):
    resp = requests.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}", **_IMMUTABLE_ID_HEADER},
        timeout=10,
    )
    resp.raise_for_status()
    for item in resp.json().get("value", []):
        if item.get("id") == message_id:
            return item
    return None


def _fetch_message_via_delta(message_id, token):
    """
    First fallback when the direct by-ID GET fails. Graph has occasionally
    404'd a just-arrived message on the single-item endpoint for a brief
    window right after notifying about it (see PR #4's notes on "message
    not found - likely deleted or moved") even though the message already
    shows up in folder-level views like delta. So instead of giving up,
    check whether it's there.
    """
    return _find_message_in(
        f"https://graph.microsoft.com/v1.0/users/{TARGET_MAILBOX}/mailFolders('Inbox')/messages/delta",
        {"$select": _GRAPH_MESSAGE_SELECT, "$top": 25},
        message_id,
        token,
    )


def _fetch_message_via_search(message_id, token):
    """
    Second fallback if delta doesn't turn the message up either: a plain
    listing of the Inbox's most recent messages, newest first. Same
    rationale as the delta fallback above - a different Graph read path
    that may already be consistent when the single-item GET isn't yet.
    """
    return _find_message_in(
        f"https://graph.microsoft.com/v1.0/users/{TARGET_MAILBOX}/mailFolders('Inbox')/messages",
        {"$select": _GRAPH_MESSAGE_SELECT, "$orderby": "receivedDateTime desc", "$top": 25},
        message_id,
        token,
    )


def _fetch_and_cache_message(message_id):
    """
    Fetches the message straight from Microsoft Graph and saves it to
    graph_message_cache, as the very first thing done for a newly-seen
    message - before firing the routine, which itself takes several more
    seconds just to start a session. On this live dispatch inbox, staff
    routinely read/move/delete a new email within seconds of it arriving,
    so a routine run that looks the message up *itself* once it finally
    starts is racing staff and can easily lose. Fetching here, directly
    against Graph with our own app-only token (no session startup delay),
    is as fast as this app can possibly capture it.

    Tries the direct by-ID GET first; if that fails, falls back to a delta
    query and then a recent-listing search of the Inbox, both scanned for
    a matching id (see _fetch_message_via_delta/_fetch_message_via_search).

    Returns a formatted text block for fire_routine() to embed in the
    routine's trigger payload, or None if every fetch attempt failed (e.g.
    the message is genuinely gone, or a Graph/token error) - the routine
    falls back to a single live lookup attempt in that case.
    """
    try:
        token = get_graph_token()
    except Exception:
        app.logger.exception("Failed to get a Graph token for message_id=%s.", message_id)
        return None

    data = None
    try:
        data = _fetch_message_by_id(message_id, token)
    except Exception:
        app.logger.warning(
            "Direct by-ID fetch failed for message_id=%s - trying delta/search fallback.",
            message_id,
        )
        for fallback in (_fetch_message_via_delta, _fetch_message_via_search):
            try:
                data = fallback(message_id, token)
            except Exception:
                app.logger.exception(
                    "Fallback fetch %s raised for message_id=%s.", fallback.__name__, message_id
                )
                data = None
            if data:
                break

    if not data:
        app.logger.error(
            "Could not fetch message_id=%s from Graph - by-ID, delta, and search fallback all failed.",
            message_id,
        )
        return None

    sender = (data.get("from") or {}).get("emailAddress") or {}
    body = data.get("body") or {}
    sender_name = sender.get("name", "")
    sender_address = sender.get("address", "")
    subject = data.get("subject", "")
    body_content_type = body.get("contentType", "")
    body_content = body.get("content", "")
    received_datetime = data.get("receivedDateTime", "")
    is_read = 1 if data.get("isRead") else 0

    conn = get_db()
    conn.execute(
        """
        INSERT INTO graph_message_cache
            (message_id, sender_name, sender_address, subject, body_content_type, body_content, received_datetime, is_read, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            sender_name = excluded.sender_name,
            sender_address = excluded.sender_address,
            subject = excluded.subject,
            body_content_type = excluded.body_content_type,
            body_content = excluded.body_content,
            received_datetime = excluded.received_datetime,
            is_read = excluded.is_read,
            fetched_at = excluded.fetched_at
        """,
        (
            message_id,
            sender_name,
            sender_address,
            subject,
            body_content_type,
            body_content,
            received_datetime,
            is_read,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    return (
        f"From: {sender_name} <{sender_address}>\n"
        f"Subject: {subject}\n"
        f"Received: {received_datetime}\n"
        f"isRead at capture time: {bool(is_read)}\n"
        f"Body ({body_content_type}):\n{body_content}"
    )


def _mark_seen_once(message_id):
    """
    Returns True the first time this message_id is seen, False on any
    repeat. Backed by the database (not an in-memory set) so dedup still
    works if duplicate notifications land in separate requests/threads, or
    across a restart between them - a plain in-process set would not
    survive either of those.
    """
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO graph_webhook_seen (message_id, first_seen_at) VALUES (?, ?)",
            (message_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


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


STARTUP_GRACE_PERIOD = timedelta(seconds=40)


def _start_scheduler():
    """
    Runs ensure_subscription_fresh() shortly after startup and then once a
    day, so the Graph subscription renews itself well before its ~3-day
    expiration without needing a separate scheduled job elsewhere.

    The first run is delayed by STARTUP_GRACE_PERIOD rather than firing
    immediately. Creating a subscription makes Graph immediately call back
    into this same app's /ms-graph/webhook to validate it - if that first
    run fires the instant this module is imported, it can race Render's own
    startup (the app isn't necessarily listening/routable yet), and Graph's
    validation callback gets a 502 instead of a 200. This delay just gives
    the app a chance to be fully up and reachable first.
    """
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        ensure_subscription_fresh,
        "interval",
        hours=24,
        next_run_time=datetime.now(timezone.utc) + STARTUP_GRACE_PERIOD,
    )
    scheduler.start()
    return scheduler


@app.route("/leads-status", methods=["GET"])
@requires_auth
def get_all_lead_status():
    """Returns the saved status (New/Contacted/Not interested) for every lead."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM leads_status").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/leads-status/<path:lead_key>", methods=["POST"])
@requires_auth
def save_lead_status(lead_key):
    """
    Saves or updates the status/notes/follow-up date/assigned rep for one
    lead (a delivery consignee that isn't already a customer). Expects
    JSON body, e.g.:
        {"status": "interested", "marked_by": "Daniel", "notes": "Left voicemail",
         "next_followup": "2026-08-20", "assigned_rep": "DANIEL G WEATHERS"}
    Any field left out keeps its previous saved value.
    """
    body = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    existing = conn.execute(
        "SELECT status, marked_by, notes, next_followup, assigned_rep FROM leads_status WHERE lead_key = ?",
        (lead_key,),
    ).fetchone()

    status = body["status"] if "status" in body else (existing["status"] if existing else "")
    marked_by = body["marked_by"] if "marked_by" in body else (existing["marked_by"] if existing else "")
    notes = body["notes"] if "notes" in body else (existing["notes"] if existing else "")
    next_followup = body["next_followup"] if "next_followup" in body else (existing["next_followup"] if existing else "")
    assigned_rep = body["assigned_rep"] if "assigned_rep" in body else (existing["assigned_rep"] if existing else "")

    conn.execute(
        """
        INSERT INTO leads_status (lead_key, status, marked_by, notes, next_followup, assigned_rep, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lead_key) DO UPDATE SET
            status = excluded.status,
            marked_by = excluded.marked_by,
            notes = excluded.notes,
            next_followup = excluded.next_followup,
            assigned_rep = excluded.assigned_rep,
            updated_at = excluded.updated_at
        """,
        (
            lead_key,
            status,
            marked_by,
            notes,
            next_followup,
            assigned_rep,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "lead_key": lead_key})


@app.route("/leads-status/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_leads_status(_subpath):
    return "", 204


@app.route("/deleted-leads", methods=["GET"])
@requires_auth
def get_all_deleted_leads():
    """Returns every lead_key that's been permanently deleted/dismissed."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM deleted_leads").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/deleted-leads/<path:lead_key>", methods=["POST"])
@requires_auth
def delete_lead(lead_key):
    """
    Permanently dismisses a lead so it never resurfaces (whether it's a
    delivery-location lead or a claimed customer lead). Expects JSON body:
        {"deleted_by": "Gene"}
    """
    body = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    conn.execute(
        """
        INSERT INTO deleted_leads (lead_key, deleted_by, deleted_at)
        VALUES (?, ?, ?)
        ON CONFLICT(lead_key) DO UPDATE SET
            deleted_by = excluded.deleted_by,
            deleted_at = excluded.deleted_at
        """,
        (lead_key, body.get("deleted_by", ""), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "lead_key": lead_key})


@app.route("/deleted-leads/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_deleted_leads(_subpath):
    return "", 204


@app.route("/manual-leads", methods=["GET"])
@requires_auth
def get_all_manual_leads():
    """Returns every hand-typed lead (not derived from Aljex data)."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM manual_leads ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/manual-leads", methods=["POST"])
@requires_auth
def create_manual_lead():
    """
    Creates a new hand-typed lead. Expects JSON body, e.g.:
        {"name": "Acme Foods", "city": "Tampa", "state": "FL",
         "address": "100 Main St", "contact": "Jane Doe",
         "phone": "813-555-1234", "created_by": "Daniel G Weathers"}
    Only "name" is required. Returns the new row, including its id, so the
    caller can build the "MANUAL:<id>" lead key right away.
    """
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO manual_leads (name, city, state, address, contact, phone, email, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            body.get("city", ""),
            body.get("state", ""),
            body.get("address", ""),
            body.get("contact", ""),
            body.get("phone", ""),
            body.get("email", ""),
            body.get("created_by", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    new_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM manual_leads WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@app.route("/manual-leads/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_manual_leads(_subpath):
    return "", 204


@app.route("/customer-assignments", methods=["GET"])
@requires_auth
def get_all_customer_assignments():
    """Returns the saved rep assignment for every customer that's been assigned."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM customer_assignments").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/customer-assignments/<customer_id>", methods=["POST"])
@requires_auth
def save_customer_assignment(customer_id):
    """
    Assigns a customer (one that has no Sales Rep set in Aljex) to a rep.
    This does NOT change anything in Aljex itself — it's a local override
    stored here so the customer shows up under that rep's "My Customers" tab.
    Expects JSON body, e.g.:
        {"assigned_rep": "DANIEL G WEATHERS", "assigned_by": "Gene"}
    """
    body = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    conn.execute(
        """
        INSERT INTO customer_assignments (customer_id, assigned_rep, assigned_by, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            assigned_rep = excluded.assigned_rep,
            assigned_by = excluded.assigned_by,
            updated_at = excluded.updated_at
        """,
        (
            customer_id,
            body.get("assigned_rep", ""),
            body.get("assigned_by", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "customer_id": customer_id})


@app.route("/customer-assignments/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_customer_assignments(_subpath):
    return "", 204


# ---------------------------------------------------------------------
# Service rep endpoints
# ---------------------------------------------------------------------
@app.route("/service-assignments", methods=["GET"])
@requires_auth
def get_all_service_assignments():
    """Returns the saved service-rep assignment for every customer that has one."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM service_assignments").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/service-assignments/<customer_id>", methods=["POST"])
@requires_auth
def save_service_assignment(customer_id):
    """
    Assigns a customer to a service rep (the person who does day-to-day
    check-in calls, separate from whoever the sales rep is). Expects
    JSON body, e.g.:
        {"service_rep": "BUD MEALOR", "assigned_by": "Gene"}
    """
    body = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    conn.execute(
        """
        INSERT INTO service_assignments (customer_id, service_rep, assigned_by, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            service_rep = excluded.service_rep,
            assigned_by = excluded.assigned_by,
            updated_at = excluded.updated_at
        """,
        (
            customer_id,
            body.get("service_rep", ""),
            body.get("assigned_by", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "customer_id": customer_id})


@app.route("/service-assignments/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_service_assignments(_subpath):
    return "", 204


@app.route("/service-notes", methods=["GET"])
@requires_auth
def get_all_service_notes():
    """Returns all saved service-rep touch-tracking notes (one row per customer)."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM service_notes").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/service-notes/<customer_id>", methods=["POST"])
@requires_auth
def save_service_note(customer_id):
    """
    Saves or updates the service-rep fields for one customer. Expects
    JSON body, e.g.:
        {"last_touched": "2026-08-10", "next_touch_date": "2026-08-17",
         "next_action": "Call", "notes": "Checking in Monday"}
    Any field left out keeps its previous saved value.
    """
    body = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    existing = conn.execute(
        "SELECT last_touched, next_touch_date, next_action, notes FROM service_notes WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()

    def pick(field):
        if field in body:
            return body[field]
        return existing[field] if existing else ""

    conn.execute(
        """
        INSERT INTO service_notes (customer_id, last_touched, next_touch_date, next_action, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            last_touched = excluded.last_touched,
            next_touch_date = excluded.next_touch_date,
            next_action = excluded.next_action,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            customer_id,
            pick("last_touched"),
            pick("next_touch_date"),
            pick("next_action"),
            pick("notes"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "customer_id": customer_id})


@app.route("/service-notes/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_service_notes(_subpath):
    return "", 204


@app.route("/customer-contacts", methods=["GET"])
@requires_auth
def get_all_customer_contacts():
    """Returns every saved contact for every customer."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM customer_contacts ORDER BY is_primary DESC, id ASC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/customer-contacts/<customer_id>", methods=["POST"])
@requires_auth
def modify_customer_contacts(customer_id):
    """
    Add, update, delete, or re-prioritize a contact for a customer.
    Expects JSON body with an "action" field:
        {"action": "add", "name": "...", "phone": "...", "email": "...", "is_primary": true}
        {"action": "update", "contact_id": 12, "name": "...", "phone": "...", "email": "..."}
        {"action": "delete", "contact_id": 12}
        {"action": "make_primary", "contact_id": 12}
    """
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action", "add")

    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    if action == "add":
        is_primary = 1 if body.get("is_primary") else 0
        if is_primary:
            conn.execute("UPDATE customer_contacts SET is_primary = 0 WHERE customer_id = ?", (customer_id,))
        cur = conn.execute(
            """
            INSERT INTO customer_contacts (customer_id, name, phone, email, is_primary, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (customer_id, body.get("name", ""), body.get("phone", ""), body.get("email", ""), is_primary, now),
        )
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "contact_id": new_id})

    elif action == "update":
        contact_id = body.get("contact_id")
        conn.execute(
            """
            UPDATE customer_contacts SET name = ?, phone = ?, email = ?, updated_at = ?
            WHERE id = ? AND customer_id = ?
            """,
            (body.get("name", ""), body.get("phone", ""), body.get("email", ""), now, contact_id, customer_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})

    elif action == "delete":
        contact_id = body.get("contact_id")
        conn.execute(
            "DELETE FROM customer_contacts WHERE id = ? AND customer_id = ?",
            (contact_id, customer_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})

    elif action == "make_primary":
        contact_id = body.get("contact_id")
        conn.execute("UPDATE customer_contacts SET is_primary = 0 WHERE customer_id = ?", (customer_id,))
        conn.execute(
            "UPDATE customer_contacts SET is_primary = 1, updated_at = ? WHERE id = ? AND customer_id = ?",
            (now, contact_id, customer_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})

    conn.close()
    return jsonify({"error": "unknown action"}), 400


@app.route("/customer-contacts/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_customer_contacts(_subpath):
    return "", 204


@app.route("/bulk-import-service-data", methods=["POST"])
@requires_auth
def bulk_import_service_data():
    """
    One-time import used to seed service-rep assignments, notes, and
    contacts from the existing MONSTER CRM.xlsx spreadsheet. Expects a
    JSON array, one object per customer:
        {
          "customer_id": "101376",
          "service_rep": "BUD MEALOR",
          "assigned_by": "CRM Import",
          "last_touched": "2026-08-10",
          "next_touch_date": "",
          "next_action": "",
          "notes": "",
          "contacts": [
            {"name": "Matt Wang", "phone": "...", "email": "...", "is_primary": true},
            {"name": "Tony", "phone": "...", "email": "...", "is_primary": false}
          ]
        }
    Safe to re-run: assignments/notes upsert by customer_id, and contacts
    are only inserted if that customer has no contacts yet (so re-running
    this doesn't create duplicate contact rows).
    """
    records = request.get_json(force=True, silent=True)
    if not isinstance(records, list):
        return jsonify({"error": "Expected a JSON array of records"}), 400

    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    skipped = 0

    for rec in records:
        customer_id = rec.get("customer_id", "")
        if not customer_id:
            skipped += 1
            continue

        conn.execute(
            """
            INSERT INTO service_assignments (customer_id, service_rep, assigned_by, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(customer_id) DO UPDATE SET
                service_rep = excluded.service_rep,
                assigned_by = excluded.assigned_by,
                updated_at = excluded.updated_at
            """,
            (customer_id, rec.get("service_rep", ""), rec.get("assigned_by", "CRM Import"), now),
        )

        conn.execute(
            """
            INSERT INTO service_notes (customer_id, last_touched, next_touch_date, next_action, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_id) DO UPDATE SET
                last_touched = excluded.last_touched,
                next_touch_date = excluded.next_touch_date,
                next_action = excluded.next_action,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                customer_id,
                rec.get("last_touched", ""),
                rec.get("next_touch_date", ""),
                rec.get("next_action", ""),
                rec.get("notes", ""),
                now,
            ),
        )

        existing_count = conn.execute(
            "SELECT COUNT(*) as c FROM customer_contacts WHERE customer_id = ?", (customer_id,)
        ).fetchone()["c"]

        if existing_count == 0:
            for contact in rec.get("contacts", []):
                conn.execute(
                    """
                    INSERT INTO customer_contacts (customer_id, name, phone, email, is_primary, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        customer_id,
                        contact.get("name", ""),
                        contact.get("phone", ""),
                        contact.get("email", ""),
                        1 if contact.get("is_primary") else 0,
                        now,
                    ),
                )

        imported += 1

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "imported": imported, "skipped": skipped})


@app.route("/bulk-import-service-data", methods=["OPTIONS"])
def cors_preflight_bulk_import_service():
    return "", 204


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
_scheduler = _start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
