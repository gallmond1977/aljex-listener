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


def fire_routine(messages):
    """
    Kicks off a single on-demand run of the carrier-auto-respond routine,
    telling it exactly which inbound message(s) just arrived (via one
    REALTIME_TRIGGER marker per message in the `text` field) instead of
    letting it fall back to re-scanning the last 75 minutes of mail. See
    the routine's own prompt (claude.ai/code/routines) for how it uses
    this.

    `messages` is a list of dicts, each with "message_id" and optionally
    "content" / "received_at" - normally one per Graph notification, but
    app.py's notification coalescing can pass several here at once when
    multiple messages arrived within the same short debounce window, so
    they're handled by one routine session instead of several concurrent
    ones. The common case is a list of exactly one, which produces the same
    single-block text as before.

    Each entry's content, if given, is that message's subject/sender/body
    as fetched by app.py's _fetch_and_cache_message() immediately on
    arrival, and is embedded directly in the payload so the routine can use
    it as-is instead of looking the message up itself once its own session
    finally starts a few seconds later - by which point staff on this live
    dispatch inbox may have already read, moved, or deleted it.

    Fire-and-forget: logs on failure but never raises. This is meant to be
    called from a background thread after the webhook route has already
    responded to Microsoft Graph, so there's no one left to hand an
    exception to.
    """
    if not messages:
        return

    if not (CLAUDE_ROUTINE_TOKEN and CLAUDE_ROUTINE_FIRE_URL):
        log.error("CLAUDE_ROUTINE_TOKEN / CLAUDE_ROUTINE_FIRE_URL not configured - cannot fire routine.")
        return

    message_ids = [m["message_id"] for m in messages]

    blocks = []
    for m in messages:
        block = f"REALTIME_TRIGGER: message_id={m['message_id']}"
        if m.get("received_at"):
            block += f" received_at={m['received_at']}"
        if m.get("content"):
            block += (
                "\n\nCAPTURED_MESSAGE_CONTENT (fetched by the webhook the moment this "
                "message arrived, before staff had a chance to act on it - use this "
                "directly instead of looking the message up yourself):\n" + m["content"]
            )
        blocks.append(block)
    text = "\n\n---\n\n".join(blocks)

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
            log.error("Routine fire failed for message_ids=%s: %s %s", message_ids, resp.status_code, resp.text[:500])
        else:
            session_url = resp.json().get("claude_code_session_url", "")
            log.info("Routine fired for message_ids=%s -> %s", message_ids, session_url)
    except Exception:
        log.exception("Routine fire raised an exception for message_ids=%s", message_ids)
