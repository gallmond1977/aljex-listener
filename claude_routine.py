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

# Used when a retryable failure (429/5xx/network error) doesn't come with a
# usable Retry-After header - e.g. a network error, or a malformed header.
# The documented 429 for this endpoint is a per-account *daily* run/usage
# allowance, not a short per-minute window, so a Retry-After is normally
# present and this is just a safety fallback.
DEFAULT_RETRY_SECONDS = 60.0

log = logging.getLogger("claude_routine")


def _parse_retry_after(value):
    if not value:
        return DEFAULT_RETRY_SECONDS
    try:
        return max(float(value), 1.0)
    except (TypeError, ValueError):
        # Retry-After may also be an HTTP-date rather than a delta-seconds
        # value; this app has no need to parse that format precisely, so
        # just fall back to the default backoff instead of guessing.
        return DEFAULT_RETRY_SECONDS


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

    Never raises - this is meant to be called from a background thread
    after the webhook route has already responded to Microsoft Graph, so
    there's no one left to hand an exception to. Returns one of:
      - True: fired successfully (or there was nothing to fire).
      - False: failed for a reason that won't improve on retry (bad
        config, or a 4xx other than 429) - the caller shouldn't requeue
        this.
      - a float: failed for a *retryable* reason (429 rate/usage limit,
        5xx, or a network error) - the number of seconds the caller should
        wait before retrying the same batch, taken from the 429's
        Retry-After header when present.
    """
    if not messages:
        return True

    if not (CLAUDE_ROUTINE_TOKEN and CLAUDE_ROUTINE_FIRE_URL):
        log.error("CLAUDE_ROUTINE_TOKEN / CLAUDE_ROUTINE_FIRE_URL not configured - cannot fire routine.")
        return False

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
    except Exception:
        log.exception("Routine fire raised an exception for message_ids=%s - will retry.", message_ids)
        return DEFAULT_RETRY_SECONDS

    if resp.status_code == 429 or resp.status_code >= 500:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        log.error(
            "Routine fire throttled/unavailable for message_ids=%s: %s %s - retrying in %.0fs.",
            message_ids,
            resp.status_code,
            resp.text[:500],
            retry_after,
        )
        return retry_after

    if resp.status_code >= 400:
        log.error(
            "Routine fire failed for message_ids=%s: %s %s - not retrying.",
            message_ids,
            resp.status_code,
            resp.text[:500],
        )
        return False

    session_url = resp.json().get("claude_code_session_url", "")
    log.info("Routine fired for message_ids=%s -> %s", message_ids, session_url)
    return True
