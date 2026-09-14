"""
Aljex -> GitHub open-loads sync
--------------------------------
Standalone script, meant to run as its own Render Cron Job (every few
minutes) - NOT part of the aljex-listener web service and NOT a Claude Code
routine. It has nothing to do with the carrier-auto-respond email workflow;
it exists to give a different bot (Grok, on the loads@ first-replies
workflow) a small, fast, always-current JSON snapshot of open freight it can
read without hitting Aljex or Claude itself.

What it does, each run:
1. Reads every "loads" record already synced into this repo's own
   aljex-listener service (Aljex pushes updates there in real time via its
   Live Sync webhook - see app.py's /aljex-webhook - so this is already
   "live" Aljex data, just one hop away instead of a second direct Aljex
   integration).
2. Picks: all OPEN loads, all HOLD loads, and any other-status load from the
   last RECENT_LANE_DAYS days that shares a lane (same pickup city/state ->
   delivery city/state) with a current OPEN load - so Grok can recognize
   "that load is covered" without a live lookup.
3. Writes the result to loads/open-loads.json in this GitHub repo via the
   GitHub Contents API (create or update, in place - no local git needed,
   which matters here since a Render Cron Job's filesystem is thrown away
   after each run).

Configuration (Render > this cron job's service > Environment):
    ALJEX_LISTENER_URL  - base URL of the aljex-listener service.
                           Defaults to https://aljex-listener.onrender.com
    SYNC_USERNAME        - same Basic Auth username the listener's
    SYNC_PASSWORD          /records/<table> endpoints already require.
    GITHUB_TOKEN         - a token (classic or fine-grained) with Contents:
                           write access to this repo, used to commit
                           loads/open-loads.json.
    GITHUB_REPO          - "owner/repo". Defaults to
                           gallmond1977/aljex-listener
    GITHUB_BRANCH        - defaults to "main"
    RECENT_LANE_DAYS      - how many days back to look for same-lane
                           non-OPEN loads. Defaults to 7.

Field mapping was confirmed against a real Aljex load record (not guessed):
    pro            <- id
    status         <- status              (OPEN/HOLD/COVERED/DISPATCHED/...)
    pu_city/state  <- origin_city/origin_state
    pu_hours       <- pickup_hours
    del_city/state <- dest_city/dest_state
    del_hours      <- consignee_hours
    weight_lbs     <- weight
    carrier_rate   <- carrier_line_haul   (NOT carrier_total_rate)
    equipment      <- equipment code, translated via EQUIPMENT_CODES below

del_date uses delivery_date if the load has actually delivered (that field
only populates post-delivery), otherwise falls back to must_del_date (the
target/scheduled delivery date) - same rule applied to pu_date/pickup_date
in case a load is ever missing it, though pickup_date has been confirmed to
populate correctly pre-pickup.

updated_at is this listener's own received_at timestamp for the record
(when the webhook last recorded a change) - Aljex's raw payload doesn't
carry a confirmed last-modified field of its own.
"""

import base64
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sync_open_loads")

ALJEX_LISTENER_URL = os.environ.get("ALJEX_LISTENER_URL", "https://aljex-listener.onrender.com")
SYNC_USERNAME = os.environ.get("SYNC_USERNAME", "")
SYNC_PASSWORD = os.environ.get("SYNC_PASSWORD", "")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "gallmond1977/aljex-listener")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
OUTPUT_PATH = "loads/open-loads.json"

RECENT_LANE_DAYS = int(os.environ.get("RECENT_LANE_DAYS", "7"))

OPEN_LOADS_STATUS = "OPEN"
HOLD_LOADS_STATUS = "HOLD"

# Confirmed from Aljex's own equipment-type admin list (Type/Group/
# Description) - not guessed from general trucking-industry conventions,
# since Aljex's internal codes don't necessarily match those. Any code seen
# in real data that isn't in this table is passed through unchanged (see
# _equipment_name) rather than invented.
EQUIPMENT_CODES = {
    "CNST": "Conestoga",
    "CONT": "Container",
    "F": "Flatbed",
    "FL": "Flatbed Hot Shot",
    "FM": "Flatbed Team",
    "FS": "Flatbed w/ Sides",
    "FT": "Flatbed w/ Tarps",
    "LTL": "Less Than Truckload",
    "PO": "Power Only",
    "QUOT": "Quote",
    "R": "Reefer",
    "RGN": "Removable Gooseneck",
    "RM": "Reefer Team",
    "RZ": "Reefer Hazmat",
    "SAME": "Repeat Past Shipment",
    "SB": "26' Straight Box Truck",
    "SD": "Step or Drop Deck",
    "SPOT": "Spot Shipment",
    "SPTV": "Sprinter Van",
    "STOR": "Storage",
    "TL": "Truckload",
    "TORD": "Truck Ordered Not Used",
    "V": "Van",
    "VA": "Van Air-Ride",
    "VC": "Curtain Van",
    "VM": "Van Team",
    "VR": "Van or Reefer",
    "VZ": "Van Hazmat",
    "WGD": "White Glove Delivery",
}

# These are internal Aljex placeholders (a quote not yet turned into a load,
# a "repeat past shipment" marker, a truck-ordered-not-used record, storage)
# rather than real bookable freight - confirmed by Gene, not inferred from
# the descriptions alone. Any record with one of these equipment codes is
# dropped entirely rather than shown as an OPEN/HOLD/etc. load.
PLACEHOLDER_EQUIPMENT_CODES = {"QUOT", "SAME", "TORD", "STOR"}


# ---------------------------------------------------------------------
# Fetch from the aljex-listener service (already-live Aljex data)
# ---------------------------------------------------------------------
def fetch_load_records():
    if not (SYNC_USERNAME and SYNC_PASSWORD):
        raise RuntimeError("SYNC_USERNAME and SYNC_PASSWORD must both be set.")

    resp = requests.get(
        f"{ALJEX_LISTENER_URL}/records/loads",
        params={"limit": "all"},
        auth=(SYNC_USERNAME, SYNC_PASSWORD),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------
# Field mapping / transformation
# ---------------------------------------------------------------------
def _equipment_name(code):
    if not code:
        return ""
    code = str(code).strip().upper()
    return EQUIPMENT_CODES.get(code, code)


def _parse_number(raw):
    """Strips $ and , from a raw value and returns an int/float, or None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        cleaned = str(raw).replace("$", "").replace(",", "").strip()
        if not cleaned:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
    return int(value) if value == int(value) else round(value, 2)


def _make_lane(pu_city, pu_state, del_city, del_state):
    return f"{pu_city} {pu_state}-{del_city} {del_state}".strip()


def _load_fields(record):
    """
    Returns the actual load fields (id, status, origin_city, ...) for one
    /records/loads item. Most records have them flat under "data" (how the
    live Aljex webhook stores a sync'd record), but some carry them one
    level deeper still, under a nested "data" key inside that - confirmed
    against real records currently in the database, which is why almost
    every record was being skipped as "missing id or status": data.get("id")
    was reaching the outer wrapper, not the load fields themselves. Both
    shapes are handled here rather than assuming one.
    """
    outer = record.get("data") or {}
    inner = outer.get("data")
    return inner if isinstance(inner, dict) else outer


def transform_record(record):
    """
    Maps one raw aljex_records row (as returned by /records/loads) into this
    feed's output shape. Returns None (and logs why) for a record that's
    missing its pro/id or its status - both are required to sort/filter on,
    and a load record without either is malformed rather than something to
    guess values for.
    """
    data = _load_fields(record)

    pro = data.get("id")
    status = data.get("status")
    if not pro or not status:
        log.warning("Skipping malformed load record (missing id or status): record_id=%s", record.get("record_id"))
        return None

    status = str(status).strip().upper()

    raw_equipment = str(data.get("equipment") or "").strip().upper()
    if raw_equipment in PLACEHOLDER_EQUIPMENT_CODES:
        return None

    pu_city = data.get("origin_city") or ""
    pu_state = data.get("origin_state") or ""
    del_city = data.get("dest_city") or ""
    del_state = data.get("dest_state") or ""

    pu_date = data.get("pickup_date") or data.get("must_pickup_date") or ""
    del_date = data.get("delivery_date") or data.get("must_del_date") or ""

    return {
        "pro": pro,
        "status": status,
        "pu_date": pu_date,
        "pu_city": pu_city,
        "pu_state": pu_state,
        "pu_hours": data.get("pickup_hours") or "",
        "del_date": del_date,
        "del_city": del_city,
        "del_state": del_state,
        "del_hours": data.get("consignee_hours") or "",
        "equipment": _equipment_name(raw_equipment),
        "weight_lbs": _parse_number(data.get("weight")),
        "carrier_rate": _parse_number(data.get("carrier_line_haul")),
        "updated_at": record.get("received_at") or "",
        "lane": _make_lane(pu_city, pu_state, del_city, del_state),
    }


def select_rows(records):
    """
    Applies the OPEN / HOLD / recent-same-lane selection rule described in
    this module's docstring. `records` is the raw list from
    fetch_load_records(); returns the final list of output rows.
    """
    transformed = [t for t in (transform_record(r) for r in records) if t is not None]

    open_rows = [r for r in transformed if r["status"] == OPEN_LOADS_STATUS]
    hold_rows = [r for r in transformed if r["status"] == HOLD_LOADS_STATUS]
    open_lanes = {r["lane"].casefold() for r in open_rows if r["pu_city"] and r["del_city"]}

    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_LANE_DAYS)

    def is_recent(row):
        raw = row["updated_at"]
        if not raw:
            return False
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts >= cutoff

    same_lane_rows = [
        r
        for r in transformed
        if r["status"] not in (OPEN_LOADS_STATUS, HOLD_LOADS_STATUS)
        and r["lane"].casefold() in open_lanes
        and is_recent(r)
    ]

    rows = open_rows + hold_rows + same_lane_rows
    rows.sort(key=lambda r: (r["status"] != OPEN_LOADS_STATUS, r["status"] != HOLD_LOADS_STATUS, r["pu_date"], r["pro"]))
    return rows


# ---------------------------------------------------------------------
# Write to GitHub via the Contents API (no local git checkout needed -
# a Render Cron Job's disk doesn't persist between runs anyway)
# ---------------------------------------------------------------------
def push_to_github(rows):
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN must be set.")

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{OUTPUT_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    existing = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=15)
    sha = existing.json().get("sha") if existing.status_code == 200 else None

    content = json.dumps(rows, indent=2) + "\n"
    payload = {
        "message": f"Sync open loads ({len(rows)} rows, {datetime.now(timezone.utc).isoformat(timespec='seconds')})",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()


def main():
    try:
        records = fetch_load_records()
    except Exception:
        log.exception("Failed to fetch load records from %s.", ALJEX_LISTENER_URL)
        sys.exit(1)

    rows = select_rows(records)
    log.info("Selected %d rows to write to %s.", len(rows), OUTPUT_PATH)

    try:
        push_to_github(rows)
    except Exception:
        log.exception("Failed to push %s to %s@%s.", OUTPUT_PATH, GITHUB_REPO, GITHUB_BRANCH)
        sys.exit(1)

    log.info("Done.")


if __name__ == "__main__":
    main()
