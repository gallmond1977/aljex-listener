def _ensure_hidden_customers_table(conn):
    """
    Creates the roll-up table on first use. Kept here rather than in
    init_crm_db so this whole feature is one self-contained block at the
    bottom of the file — nothing else in this module has to change.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hidden_customers (
            customer_id TEXT PRIMARY KEY,
            rolled_into TEXT,
            hidden_by TEXT,
            hidden_at TEXT
        )
        """
    )


@crm_bp.route("/hidden-customers", methods=["GET"])
@requires_auth
def get_hidden_customers():
    """Returns every branch record that's been rolled up under a corporate account."""
    conn = get_db()
    _ensure_hidden_customers_table(conn)
    rows = conn.execute("SELECT * FROM hidden_customers ORDER BY hidden_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@crm_bp.route("/roll-up-branch", methods=["POST"])
@requires_auth
def roll_up_branch():
    """
    Files a branch location's contact info under its corporate customer
    number, so one corporate record carries a contact row per branch
    instead of each branch sitting in the list as its own customer.

    Only CRM-side data moves: saved contact rows (customer_contacts) and
    the branch's own touch tracking (service_notes). Nothing in Aljex is
    read or changed, and the branch's Aljex record keeps syncing exactly
    as before — it just stops being listed separately.

    Expects JSON body:
        {"branch_id": "102687", "corporate_id": "102683",
         "branch_label": "Atlanta", "rolled_up_by": "Gene"}

    branch_label is appended to each moved contact's name ("Sandy —
    Atlanta") so it's obvious which location a person belongs to once
    they're all sitting under one account. Omit it to leave names as-is.
    """
    body = request.get_json(force=True, silent=True) or {}
    branch_id = str(body.get("branch_id", "")).strip()
    corporate_id = str(body.get("corporate_id", "")).strip()
    branch_label = str(body.get("branch_label", "")).strip()
    rolled_up_by = str(body.get("rolled_up_by", "")).strip()

    if not branch_id or not corporate_id:
        return jsonify({"error": "branch_id and corporate_id are both required"}), 400
    if branch_id == corporate_id:
        return jsonify({"error": "branch_id and corporate_id are the same"}), 400

    conn = get_db()
    _ensure_hidden_customers_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    moved_contacts = []

    branch_touch = conn.execute(
        "SELECT * FROM service_notes WHERE customer_id = ?", (branch_id,)
    ).fetchone()

    contact_rows = conn.execute(
        "SELECT * FROM customer_contacts WHERE customer_id = ? ORDER BY is_primary DESC, id ASC",
        (branch_id,),
    ).fetchall()

    def labelled(name):
        base = (name or "").strip()
        if not branch_label:
            return base
        if branch_label.upper() in base.upper():
            return base
        return f"{base} — {branch_label}" if base else branch_label

    for idx, row in enumerate(contact_rows):
        # The corporate record keeps whichever contact it already had as
        # primary — an incoming branch contact never takes that slot.
        touch = {
            "last_touched": row["last_touched"],
            "next_touch_date": row["next_touch_date"],
            "next_action": row["next_action"],
            "notes": row["notes"],
        }
        # The branch's customer-level touch tracking has nowhere else to go
        # once the branch stops being listed, so it rides along on that
        # branch's first contact — but only into fields that are empty, so
        # nothing typed against the person themselves gets overwritten.
        if idx == 0 and branch_touch:
            for field in touch:
                if not touch[field] and branch_touch[field]:
                    touch[field] = branch_touch[field]

        conn.execute(
            """
            INSERT INTO customer_contacts
                (customer_id, name, phone, email, is_primary,
                 last_touched, next_touch_date, next_action, notes, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                corporate_id, labelled(row["name"]), row["phone"], row["email"],
                touch["last_touched"], touch["next_touch_date"],
                touch["next_action"], touch["notes"], now,
            ),
        )
        moved_contacts.append(labelled(row["name"]))

    # A branch with no saved contact rows still has touch history worth
    # keeping, so it becomes a contact row named after the branch itself.
    if not contact_rows and branch_touch and any(
        branch_touch[f] for f in ["last_touched", "next_touch_date", "next_action", "notes"]
    ):
        placeholder = branch_label or f"Branch {branch_id}"
        conn.execute(
            """
            INSERT INTO customer_contacts
                (customer_id, name, phone, email, is_primary,
                 last_touched, next_touch_date, next_action, notes, updated_at)
            VALUES (?, ?, '', '', 0, ?, ?, ?, ?, ?)
            """,
            (
                corporate_id, placeholder,
                branch_touch["last_touched"], branch_touch["next_touch_date"],
                branch_touch["next_action"], branch_touch["notes"], now,
            ),
        )
        moved_contacts.append(placeholder)

    conn.execute("DELETE FROM customer_contacts WHERE customer_id = ?", (branch_id,))
    conn.execute("DELETE FROM service_notes WHERE customer_id = ?", (branch_id,))

    conn.execute(
        """
        INSERT OR REPLACE INTO hidden_customers (customer_id, rolled_into, hidden_by, hidden_at)
        VALUES (?, ?, ?, ?)
        """,
        (branch_id, corporate_id, rolled_up_by, now),
    )

    conn.commit()
    conn.close()
    return jsonify({
        "status": "ok",
        "branch_id": branch_id,
        "corporate_id": corporate_id,
        "moved_contacts": moved_contacts,
    })


@crm_bp.route("/roll-up-branch/undo/<branch_id>", methods=["POST"])
@requires_auth
def undo_roll_up_branch(branch_id):
    """
    Puts a branch back in the customer list. Contacts that were moved onto
    the corporate record are left where they are — this only un-hides the
    branch, so anything moved by mistake can be deleted by hand from the
    corporate record's contact list.
    """
    conn = get_db()
    _ensure_hidden_customers_table(conn)
    conn.execute("DELETE FROM hidden_customers WHERE customer_id = ?", (str(branch_id).strip(),))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "branch_id": branch_id})


@crm_bp.route("/hidden-customers", methods=["OPTIONS"])
@crm_bp.route("/roll-up-branch", methods=["OPTIONS"])
@crm_bp.route("/roll-up-branch/<path:_subpath>", methods=["OPTIONS"])
def cors_preflight_roll_up_branch(_subpath=None):
    return "", 204
