"""
Microsoft Graph change-notification ("webhook") subscription management.

Graph subscriptions on mail expire after ~3 days max - Microsoft does not
allow a permanent one. This module creates the subscription that watches
loads@monstertrucking.com's Inbox for new mail, and renews it well before
it expires. ensure_subscription_fresh() is what the background scheduler
in app.py calls once a day (and once at startup) to do that automatically.

If renewal ever fails, this logs loudly (ERROR level, with a message meant
to be unmissable in the Render logs) rather than failing silently - see
app.py for how logging is wired up. Set HEALTHCHECK_PING_URL (e.g. a free
healthchecks.io check) to also get pinged on every renewal attempt, so a
missed daily run shows up even if the app itself were to stop running
entirely.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from db import get_db
from graph_auth import get_graph_token

TARGET_MAILBOX = os.environ.get("GRAPH_TARGET_MAILBOX", "loads@monstertrucking.com")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "https://aljex-listener.onrender.com")
GRAPH_CLIENT_STATE = os.environ.get("GRAPH_CLIENT_STATE", "")
HEALTHCHECK_PING_URL = os.environ.get("HEALTHCHECK_PING_URL", "")

# Graph caps mail subscriptions at 4230 minutes (~2.94 days). We ask for
# just under that, and renew once less than 36 hours remain - so a single
# missed daily renewal still leaves a wide safety margin before anything
# actually goes dark.
SUBSCRIPTION_LIFETIME_MINUTES = 4200
RENEW_WHEN_LESS_THAN = timedelta(hours=36)

NOTIFICATION_URL = f"{WEBHOOK_BASE_URL}/ms-graph/webhook"
GRAPH_SUBSCRIPTIONS_URL = "https://graph.microsoft.com/v1.0/subscriptions"

log = logging.getLogger("graph_subscription")


def _expiration_iso(minutes_from_now=SUBSCRIPTION_LIFETIME_MINUTES):
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return dt.isoformat().replace("+00:00", "Z")


def _parse_graph_datetime(value):
    """
    Parses a datetime string returned by Graph into an aware datetime.
    Graph can return up to 7 fractional-second digits (100ns ticks, e.g.
    "2026-08-14T13:00:00.1234567Z"), but Python's datetime.fromisoformat()
    only accepts up to 6 (microseconds) - without trimming, this raises
    ValueError on a subscription that is perfectly fine, which would make
    ensure_subscription_fresh() falsely report a renewal failure every day.
    """
    value = value.replace("Z", "+00:00")
    value = re.sub(r"\.(\d{1,9})", lambda m: "." + m.group(1)[:6], value)
    return datetime.fromisoformat(value)


def _save_state(subscription_id, expiration_datetime, status, error=None):
    """
    subscription_id / expiration_datetime may be passed as None to mean
    "don't know / didn't change" (e.g. a failed renewal attempt) - COALESCE
    keeps whatever was last known-good rather than overwriting it with
    NULL, so a single failed call can't wipe out a still-valid expiration
    date and turn into a crash the next time this runs.
    """
    conn = get_db()
    conn.execute(
        """
        INSERT INTO graph_subscription (id, subscription_id, expiration_datetime, last_checked_at, last_status, last_error)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            subscription_id = COALESCE(excluded.subscription_id, graph_subscription.subscription_id),
            expiration_datetime = COALESCE(excluded.expiration_datetime, graph_subscription.expiration_datetime),
            last_checked_at = excluded.last_checked_at,
            last_status = excluded.last_status,
            last_error = excluded.last_error
        """,
        (
            subscription_id,
            expiration_datetime,
            datetime.now(timezone.utc).isoformat(),
            status,
            error,
        ),
    )
    conn.commit()
    conn.close()


def get_subscription_state():
    conn = get_db()
    row = conn.execute("SELECT * FROM graph_subscription WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def create_subscription():
    """
    Creates a brand-new Graph subscription and stores its details.

    Sent with the Prefer: IdType="ImmutableId" header so Graph hands out
    immutable message IDs in this subscription's change notifications.
    Without it, the "id" in resourceData is a regular Outlook ID, which
    changes whenever the message moves between folders (e.g. an inbox
    rule filing it away right after it arrives) - a race that has already
    caused real "message not found - likely deleted or moved" failures on
    this inbox (see PR #4's notes). Immutable IDs stay valid across moves,
    so app.py's by-ID fetch keeps working even if the message has already
    moved by the time it runs. app.py's fetch calls must send the same
    Prefer header, since a regular ID and an immutable ID for the same
    message are different strings and aren't interchangeable.
    """
    if not GRAPH_CLIENT_STATE:
        raise RuntimeError("GRAPH_CLIENT_STATE must be set (Render > Environment) before creating a subscription.")

    token = get_graph_token()
    body = {
        "changeType": "created",
        "notificationUrl": NOTIFICATION_URL,
        "resource": f"/users/{TARGET_MAILBOX}/mailFolders('Inbox')/messages",
        "expirationDateTime": _expiration_iso(),
        "clientState": GRAPH_CLIENT_STATE,
    }
    resp = requests.post(
        GRAPH_SUBSCRIPTIONS_URL,
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Prefer": 'IdType="ImmutableId"',
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        _save_state(None, None, "error", f"create failed: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()

    data = resp.json()
    _save_state(data["id"], data["expirationDateTime"], "ok", None)
    return data


def renew_subscription(subscription_id):
    """Extends an existing subscription's expiration."""
    token = get_graph_token()
    resp = requests.patch(
        f"{GRAPH_SUBSCRIPTIONS_URL}/{subscription_id}",
        json={"expirationDateTime": _expiration_iso()},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code >= 400:
        _save_state(subscription_id, None, "error", f"renew failed: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()

    data = resp.json()
    _save_state(subscription_id, data["expirationDateTime"], "ok", None)
    return data


def _ping_healthcheck(suffix=""):
    if not HEALTHCHECK_PING_URL:
        return
    try:
        requests.get(HEALTHCHECK_PING_URL + suffix, timeout=10)
    except Exception:
        log.warning("Healthcheck ping itself failed (this is separate from subscription health).")


def ensure_subscription_fresh():
    """
    Creates a subscription if none exists yet; renews it if it's getting
    close to expiring; does nothing if it's still fresh. Never raises -
    any failure is caught, logged loudly, and (if configured) reported to
    HEALTHCHECK_PING_URL, so a broken renewal can't silently go unnoticed
    the way the old hourly-draft routine did for a week.
    """
    state = get_subscription_state()
    try:
        if not state or not state.get("subscription_id"):
            log.warning("No Graph subscription on record yet - creating one now.")
            create_subscription()
            log.info("Graph subscription created successfully.")
        else:
            expires_at = _parse_graph_datetime(state["expiration_datetime"])
            remaining = expires_at - datetime.now(timezone.utc)
            if remaining < RENEW_WHEN_LESS_THAN:
                try:
                    renew_subscription(state["subscription_id"])
                    log.info("Graph subscription renewed successfully.")
                except requests.HTTPError as exc:
                    # If the subscription already fully expired or was deleted
                    # server-side before we got to it, Graph 404s on renewal -
                    # recreate from scratch instead of failing forever on a
                    # subscription ID that no longer exists.
                    if exc.response is not None and exc.response.status_code == 404:
                        log.warning("Existing Graph subscription is gone (404) - creating a new one.")
                        create_subscription()
                        log.info("Graph subscription re-created successfully.")
                    else:
                        raise
            else:
                log.info("Graph subscription still fresh (expires %s) - no action needed.", expires_at.isoformat())
    except Exception:
        log.error(
            "!!! GRAPH SUBSCRIPTION RENEWAL FAILED !!! Real-time carrier-email "
            "triggering will go dark once the current subscription expires "
            "if this isn't fixed. Check AZURE_TENANT_ID / AZURE_CLIENT_ID / "
            "AZURE_CLIENT_SECRET and Graph connectivity.",
            exc_info=True,
        )
        _ping_healthcheck("/fail")
        return

    _ping_healthcheck()
