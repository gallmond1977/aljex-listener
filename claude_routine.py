"""
Fires the "Carrier Auto-Respond - Draft Availability Replies" Claude Code
routine on demand, instead of waiting for its hourly schedule.

Uses Claude Code's routine-fire API (experimental as of 2026-08):
https://platform.claude.com/docs/en/api/claude-code/routines-fire

Configuration (Render > your service > Environment):
    CLAUDE_ROUTINE_TOKEN     - the per-routine bearer token generated at
                               claude.ai/code/routines
    CLAUDE_ROUTINE_FIRE_URL  - the routine's fire URL, shown alongside the
                               token when it was generated
"""

import logging
import os

import requests

CLAUDE_ROUTINE_TOKEN = os.environ.get("CLAUDE_ROUTINE_TOKEN", "")
CLAUDE_ROUTINE_FIRE_URL = os.environ.get("CLAUDE_ROUTINE_FIRE_URL", "")

log = logging.getLogger("claude_routine")


def fire_routine(message_id, received_at=None):
    """
    Kicks off an on-demand run of the carrier-auto-respond routine, telling
    it exactly which inbound message just arrived (via the REALTIME_TRIGGER
    marker in the `text` field) instead of letting it fall back to
    re-scanning the last 75 minutes of mail. See the routine's own prompt
    (claude.ai/code/routines) for how it uses this.

    Fire-and-forget: logs on failure but never raises. This is meant to be
    called from a background thread after the webhook route has already
    responded to Microsoft Graph, so there's no one left to hand an
    exception to.
    """
    if not (CLAUDE_ROUTINE_TOKEN and CLAUDE_ROUTINE_FIRE_URL):
        log.error("CLAUDE_ROUTINE_TOKEN / CLAUDE_ROUTINE_FIRE_URL not configured - cannot fire routine.")
        return

    text = f"REALTIME_TRIGGER: message_id={message_id}"
    if received_at:
        text += f" received_at={received_at}"

    try:
        resp = requests.post(
            CLAUDE_ROUTINE_FIRE_URL,
            headers={
                "Authorization": f"Bearer {CLAUDE_ROUTINE_TOKEN}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "experimental-cc-routine-2026-04-01",
                "Content-Type": "application/json",
            },
            json={"text": text},
            timeout=15,
        )
        if resp.status_code >= 400:
            log.error("Routine fire failed for message_id=%s: %s %s", message_id, resp.status_code, resp.text[:500])
        else:
            session_url = resp.json().get("claude_code_session_url", "")
            log.info("Routine fired for message_id=%s -> %s", message_id, session_url)
    except Exception:
        log.exception("Routine fire raised an exception for message_id=%s", message_id)
