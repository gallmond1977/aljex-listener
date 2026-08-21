"""
Sales/service CRM: leads, quotes, customer/service rep assignments, and
contacts - split out of app.py into its own module (and its own Flask
Blueprint) so a change to one can't silently delete the other.

That's not hypothetical: three separate times, a direct "Update app.py"
edit (bypassing PRs) pasted in a version of the file that predated
whichever feature lived in the other half, deleting it. Keeping the two
in separate files doesn't make direct pushes safe, but it does mean a
paste-over of one can no longer take out the other.

Registered onto the main Flask app in app.py via:
    from crm import crm_bp, init_crm_db
    app.register_blueprint(crm_bp)
    ... and init_crm_db() is called alongside app.py's own init_db().

CORS (Access-Control-Allow-* headers, including the OPTIONS preflight
routes below) is handled by app.py's app-wide @app.after_request hook -
that applies to every response regardless of which blueprint produced it,
so it isn't duplicated here.
"""

import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, request, jsonify, Response

DATABASE_PATH = os.environ.get("DATABASE_PATH", "aljex_data.db")
SYNC_USERNAME = os.environ.get("SYNC_USERNAME", "changeme")
SYNC_PASSWORD = os.environ.get("SYNC_PASSWORD", "changeme")


def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


crm_bp = Blueprint("crm", __name__)


def init_crm_db():
    conn = get_db()
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

    # Editable contact info, stored as an override so any lead — whether it
    # came from Aljex load history or was typed in by hand — can have its
    # contact details corrected or filled in from the tool. Second contact
    # is optional (tucked away in the UI, not everyone needs two). Quote
    # fields capture what was actually quoted once a lead reaches that
    # pipeline stage (freight brokerage quote: origin, destination, mode,
    # rate).
    contact_override_columns = [
        "lead_name", "address",
        "contact_name", "contact_phone", "contact_email", "website",
        "contact_name_2", "contact_phone_2", "contact_email_2",
        "quote_origin", "quote_destination", "quote_mode", "quote_rate",
    ]
    for col in contact_override_columns:
        if col not in leads_columns:
            conn.execute(f"ALTER TABLE leads_status ADD COLUMN {col} TEXT")

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

    # A lead can have several quotes over time (different lanes, modes, or
    # just re-quoted later) — these persist as a running history rather
    # than a single overwritable set of fields.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_key TEXT,
            origin TEXT,
            destination TEXT,
            mode TEXT,
            rate TEXT,
            created_by TEXT,
            created_at TEXT
        )
        """
    )

    # One-time migration: the old single quote_origin/quote_destination/
    # quote_mode/quote_rate columns on leads_status (from before multiple
    # quotes were supported) get copied into lead_quotes so nothing typed
    # in already is lost, then are simply left unused going forward.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    already_migrated = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'quotes_migrated'"
    ).fetchone()

    if not already_migrated:
        legacy_quotes = conn.execute(
            """
            SELECT lead_key, quote_origin, quote_destination, quote_mode, quote_rate
            FROM leads_status
            WHERE (quote_origin IS NOT NULL AND quote_origin != '')
               OR (quote_destination IS NOT NULL AND quote_destination != '')
               OR (quote_mode IS NOT NULL AND quote_mode != '')
               OR (quote_rate IS NOT NULL AND quote_rate != '')
            """
        ).fetchall()
        for row in legacy_quotes:
            conn.execute(
                """
                INSERT INTO lead_quotes (lead_key, origin, destination, mode, rate, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["lead_key"], row["quote_origin"], row["quote_destination"],
                    row["quote_mode"], row["quote_rate"], "",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES ('quotes_migrated', '1')"
        )

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

    # Non-primary contacts get their own touch-tracking fields (last
    # touched, next touch, next action, notes) — same idea as the
    # customer-level ones in service_notes, but per person. Last load stays
    # customer-level only (shipping activity isn't tied to one contact).
    contact_columns = [row["name"] for row in conn.execute("PRAGMA table_info(customer_contacts)").fetchall()]
    for col in ["last_touched", "next_touch_date", "next_action", "notes"]:
        if col not in contact_columns:
            conn.execute(f"ALTER TABLE customer_contacts ADD COLUMN {col} TEXT")

    conn.commit()
    conn.close()


@crm_bp.route("/leads-status", methods=["GET"])
@requires_auth
def get_all_lead_status():
    """Returns the saved status (New/Contacted/Not interested) for every lead."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM leads_status").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/leads-status/<path:lead_key>", methods=["POST"])
@requires_auth
def save_lead_status(lead_key):
    """
    Saves or updates fields for one lead (a delivery consignee that isn't
    already a customer, or a hand-typed lead). Expects JSON body with any
    of: status, marked_by, notes, next_followup, assigned_rep, contact_name,
    contact_phone, contact_email, website, contact_name_2, contact_phone_2,
    contact_email_2, quote_origin, quote_destination, quote_mode, quote_rate.
    Any field left out keeps its previous saved value.
    """
    body = request.get_json(force=True, silent=True) or {}

    fields = [
        "status", "marked_by", "notes", "next_followup", "assigned_rep",
        "lead_name", "address",
        "contact_name", "contact_phone", "contact_email", "website",
        "contact_name_2", "contact_phone_2", "contact_email_2",
        "quote_origin", "quote_destination", "quote_mode", "quote_rate",
    ]

    conn = get_db()
    existing = conn.execute(
        f"SELECT {', '.join(fields)} FROM leads_status WHERE lead_key = ?",
        (lead_key,),
    ).fetchone()

    values = {}
    for f in fields:
        values[f] = body[f] if f in body else (existing[f] if existing else "")

    col_list = ", ".join(fields)
    placeholders = ", ".join(["?"] * len(fields))
    update_clause = ", ".join([f"{f} = excluded.{f}" for f in fields])

    conn.execute(
        f"""
        INSERT INTO leads_status (lead_key, {col_list}, updated_at)
        VALUES (?, {placeholders}, ?)
        ON CONFLICT(lead_key) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        [lead_key] + [values[f] for f in fields] + [datetime.now(timezone.utc).isoformat()],
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "lead_key": lead_key})


@crm_bp.route("/leads-status/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_leads_status(_subpath):
    return "", 204


@crm_bp.route("/deleted-leads", methods=["GET"])
@requires_auth
def get_all_deleted_leads():
    """Returns every lead_key that's been permanently deleted/dismissed."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM deleted_leads").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/deleted-leads/<path:lead_key>", methods=["POST"])
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


@crm_bp.route("/deleted-leads/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_deleted_leads(_subpath):
    return "", 204


@crm_bp.route("/manual-leads", methods=["GET"])
@requires_auth
def get_all_manual_leads():
    """Returns every hand-typed lead (not derived from Aljex data)."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM manual_leads ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/manual-leads", methods=["POST"])
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


@crm_bp.route("/manual-leads/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_manual_leads(_subpath):
    return "", 204


@crm_bp.route("/lead-quotes", methods=["GET"])
@requires_auth
def get_all_lead_quotes():
    """Returns every quote ever entered, across every lead."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM lead_quotes ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/lead-quotes", methods=["POST"])
@requires_auth
def create_lead_quote():
    """
    Adds a new quote to a lead's history. Expects JSON body, e.g.:
        {"lead_key": "CUSTOMER:105241", "origin": "Columbus, GA",
         "destination": "Atlanta, GA", "mode": "53' Dry Van",
         "rate": "$500", "created_by": "Mitchell Tucker"}
    Quotes are never overwritten — each save creates a new row, so old
    quotes stick around as a history unless explicitly deleted.
    """
    body = request.get_json(force=True, silent=True) or {}
    lead_key = (body.get("lead_key") or "").strip()
    if not lead_key:
        return jsonify({"error": "lead_key is required"}), 400

    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO lead_quotes (lead_key, origin, destination, mode, rate, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lead_key,
            body.get("origin", ""),
            body.get("destination", ""),
            body.get("mode", ""),
            body.get("rate", ""),
            body.get("created_by", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    new_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM lead_quotes WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@crm_bp.route("/lead-quotes/<int:quote_id>", methods=["POST"])
@requires_auth
def update_lead_quote(quote_id):
    """Updates one existing quote's fields. Any field left out keeps its previous value."""
    body = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    existing = conn.execute("SELECT * FROM lead_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "quote not found"}), 404

    fields = ["origin", "destination", "mode", "rate"]
    values = {f: (body[f] if f in body else existing[f]) for f in fields}

    conn.execute(
        """
        UPDATE lead_quotes SET origin = ?, destination = ?, mode = ?, rate = ?
        WHERE id = ?
        """,
        (values["origin"], values["destination"], values["mode"], values["rate"], quote_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM lead_quotes WHERE id = ?", (quote_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@crm_bp.route("/lead-quotes/<int:quote_id>", methods=["DELETE"])
@requires_auth
def delete_lead_quote(quote_id):
    conn = get_db()
    conn.execute("DELETE FROM lead_quotes WHERE id = ?", (quote_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "id": quote_id})


@crm_bp.route("/lead-quotes", methods=["OPTIONS"])
def cors_preflight_lead_quotes_root():
    return "", 204


@crm_bp.route("/lead-quotes/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_lead_quotes(_subpath):
    return "", 204


@crm_bp.route("/customer-assignments", methods=["GET"])
@requires_auth
def get_all_customer_assignments():
    """Returns the saved rep assignment for every customer that's been assigned."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM customer_assignments").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/customer-assignments/<customer_id>", methods=["POST"])
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


@crm_bp.route("/customer-assignments/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_customer_assignments(_subpath):
    return "", 204


# ---------------------------------------------------------------------
# Service rep endpoints
# ---------------------------------------------------------------------
@crm_bp.route("/service-assignments", methods=["GET"])
@requires_auth
def get_all_service_assignments():
    """Returns the saved service-rep assignment for every customer that has one."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM service_assignments").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/service-assignments/<customer_id>", methods=["POST"])
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


@crm_bp.route("/service-assignments/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_service_assignments(_subpath):
    return "", 204


@crm_bp.route("/service-notes", methods=["GET"])
@requires_auth
def get_all_service_notes():
    """Returns all saved service-rep touch-tracking notes (one row per customer)."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM service_notes").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/service-notes/<customer_id>", methods=["POST"])
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


@crm_bp.route("/service-notes/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_service_notes(_subpath):
    return "", 204


@crm_bp.route("/customer-contacts", methods=["GET"])
@requires_auth
def get_all_customer_contacts():
    """Returns every saved contact for every customer."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM customer_contacts ORDER BY is_primary DESC, id ASC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/customer-contacts/<customer_id>", methods=["POST"])
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
        row = conn.execute("SELECT * FROM customer_contacts WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return jsonify(dict(row))

    elif action == "update":
        contact_id = body.get("contact_id")
        existing = conn.execute(
            "SELECT * FROM customer_contacts WHERE id = ? AND customer_id = ?",
            (contact_id, customer_id),
        ).fetchone()
        if not existing:
            conn.close()
            return jsonify({"error": "contact not found"}), 404

        fields = ["name", "phone", "email", "last_touched", "next_touch_date", "next_action", "notes"]
        values = {f: (body[f] if f in body else existing[f]) for f in fields}

        conn.execute(
            """
            UPDATE customer_contacts SET
                name = ?, phone = ?, email = ?,
                last_touched = ?, next_touch_date = ?, next_action = ?, notes = ?,
                updated_at = ?
            WHERE id = ? AND customer_id = ?
            """,
            (
                values["name"], values["phone"], values["email"],
                values["last_touched"], values["next_touch_date"], values["next_action"], values["notes"],
                now, contact_id, customer_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM customer_contacts WHERE id = ?", (contact_id,)).fetchone()
        conn.close()
        return jsonify(dict(row))

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


@crm_bp.route("/customer-contacts/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_customer_contacts(_subpath):
    return "", 204


@crm_bp.route("/bulk-import-service-data", methods=["POST"])
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


@crm_bp.route("/bulk-import-service-data", methods=["OPTIONS"])
def cors_preflight_bulk_import_service():
    return "", 204


@crm_bp.route("/fix-customer-id", methods=["POST"])
@requires_auth
def fix_customer_id():
    """
    One-time data-repair tool: moves every record tied to a wrong customer
    ID (rep_notes, customer_assignments, service_assignments, service_notes,
    customer_contacts) over to the correct Aljex customer ID. This happens
    when a customer got matched to the wrong account during a bulk import
    (e.g. from the original MONSTER CRM.xlsx backfill) — the notes, rep
    assignment, and contacts are still good, they're just filed under the
    wrong ID, which is why load history doesn't line up.

    Expects JSON body: {"old_id": "104706", "new_id": "100591", "fixed_by": "Gene"}
    Existing data at new_id (if any) is NOT overwritten — old_id's data is
    only moved in where new_id doesn't already have something for that field.

    rep_notes lives in app.py's own database, not this module's, but that's
    just a Python-file boundary — both connect to the same SQLite file via
    DATABASE_PATH, so it's addressed here the same way as any other table.
    """
    body = request.get_json(force=True, silent=True) or {}
    old_id = str(body.get("old_id", "")).strip()
    new_id = str(body.get("new_id", "")).strip()
    if not old_id or not new_id:
        return jsonify({"error": "old_id and new_id are both required"}), 400
    if old_id == new_id:
        return jsonify({"error": "old_id and new_id are the same"}), 400

    conn = get_db()
    moved = []

    single_row_tables = ["rep_notes", "customer_assignments", "service_assignments", "service_notes"]
    for table in single_row_tables:
        old_row = conn.execute(f"SELECT * FROM {table} WHERE customer_id = ?", (old_id,)).fetchone()
        if not old_row:
            continue
        new_row = conn.execute(f"SELECT * FROM {table} WHERE customer_id = ?", (new_id,)).fetchone()
        if new_row:
            # Something already exists at the correct ID — don't clobber it.
            continue
        cols = [c for c in old_row.keys() if c != "customer_id"]
        placeholders = ", ".join(["?"] * (len(cols) + 1))
        col_list = ", ".join(["customer_id"] + cols)
        values = [new_id] + [old_row[c] for c in cols]
        conn.execute(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", values)
        conn.execute(f"DELETE FROM {table} WHERE customer_id = ?", (old_id,))
        moved.append(table)

    contact_rows = conn.execute("SELECT * FROM customer_contacts WHERE customer_id = ?", (old_id,)).fetchall()
    if contact_rows:
        conn.execute("UPDATE customer_contacts SET customer_id = ? WHERE customer_id = ?", (new_id, old_id))
        moved.append(f"customer_contacts ({len(contact_rows)} row(s))")

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "old_id": old_id, "new_id": new_id, "moved": moved})


@crm_bp.route("/fix-customer-id", methods=["OPTIONS"])
def cors_preflight_fix_customer_id():
    return "", 204
