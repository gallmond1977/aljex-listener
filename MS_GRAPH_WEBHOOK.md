# Real-time carrier-email triggering (Microsoft Graph webhook)

## What this does

Before this, the "Carrier Auto-Respond" Claude Code routine only checked
loads@monstertrucking.com's inbox on an hourly schedule. This adds a
real-time path: the moment a new email lands in that inbox, Microsoft
Graph calls this app, which immediately kicks off the routine for that
one message - instead of waiting up to an hour.

The hourly schedule still runs too, as a fallback in case this ever goes
dark (see "What happens if this breaks" below).

## How it fits together

1. **Microsoft Entra app registration** (in the monstertrucking.com
   tenant) - gives this app permission to read mail as
   loads@monstertrucking.com, with no signed-in user. Already set up;
   its credentials are the `AZURE_*` environment variables below.
2. **A Microsoft Graph subscription** - tells Graph "watch this inbox, and
   POST to `/ms-graph/webhook` here every time new mail arrives." Created
   by this app (`graph_subscription.py`), not manually in any portal.
   Graph mail subscriptions expire after ~3 days max, so this app also
   renews it automatically once a day (`ensure_subscription_fresh`, run by
   a background scheduler in `app.py`). Created with the
   `Prefer: IdType="ImmutableId"` header, so the "id" Graph puts in every
   notification is an immutable message ID - one that keeps working even
   if the message moves to another folder before this app gets to it
   (e.g. an inbox rule filing it away seconds after it arrives). Every
   subsequent fetch that uses that id (see below) sends the same header,
   since a regular ID and an immutable ID for the same message are
   different strings.
3. **`POST /ms-graph/webhook`** - what Graph actually calls. Validates the
   notification, then on a background thread (so Graph gets a fast response
   and doesn't retry): dedupes by message ID (Graph can redeliver the same
   notification), fetches that message's subject/sender/body directly from
   Graph right then and caches it (`_fetch_and_cache_message`, into
   `graph_message_cache`), and queues the message ID and that captured
   content for the Claude routine. This inbox is live - staff often
   read/move/delete a new email within seconds - so capturing content here,
   before the routine's own session even starts, is much faster than
   letting the routine look the message up itself once it gets around to
   it.

   The by-ID fetch can occasionally 404 in the brief window right after
   Graph sends the notification, even though the message is already
   visible in folder-level views - so `_fetch_and_cache_message` falls back
   to a delta query and then a recent-listing search of the Inbox
   (`_fetch_message_via_delta` / `_fetch_message_via_search`), both scanned
   for the same id, before giving up.

   Firing the routine itself is debounced rather than immediate: each
   queued message (re)arms a short timer
   (`NOTIFICATION_DEBOUNCE_SECONDS`, default 3s), and when it elapses with
   no new message having arrived, everything queued since the last fire
   goes out together as one routine session instead of one session per
   message. This only affects when the routine *starts* - content capture
   above still happens immediately per message, so the staff-race problem
   this webhook exists to avoid isn't reintroduced.
4. **The Claude routine's fire API** (`claude_routine.py`) - starts an
   on-demand run of the existing "Carrier Auto-Respond" routine, telling
   it exactly which message(s) to look at (one
   `REALTIME_TRIGGER: message_id=...` block per message, each with its own
   `CAPTURED_MESSAGE_CONTENT: ...` when available) so it doesn't need to
   re-fetch anything itself, instead of it re-scanning the last 75 minutes
   of mail. Normally this is a single message; occasionally, thanks to
   coalescing, it's a handful that arrived within the same few seconds.

## Environment variables (Render > this service > Environment)

| Variable | What it's for |
|---|---|
| `AZURE_TENANT_ID` | Entra tenant ID |
| `AZURE_CLIENT_ID` | Entra app registration's client ID |
| `AZURE_CLIENT_SECRET` | Entra app registration's client secret |
| `GRAPH_CLIENT_STATE` | Shared secret Graph echoes back on every notification, so we can tell it's really Graph calling |
| `CLAUDE_ROUTINE_TOKEN` | Per-routine bearer token from claude.ai/code/routines |
| `CLAUDE_ROUTINE_FIRE_URL` | The routine's fire URL (from the same place) |
| `GRAPH_TARGET_MAILBOX` | Optional, defaults to `loads@monstertrucking.com` |
| `WEBHOOK_BASE_URL` | Optional, defaults to `https://aljex-listener.onrender.com` |
| `HEALTHCHECK_PING_URL` | Optional - see below |
| `NOTIFICATION_DEBOUNCE_SECONDS` | Optional, defaults to `3` - how long the webhook waits for the notification burst to go quiet before firing the routine, see "How it fits together" above |

## One-time setup after this is deployed

The subscription creates itself automatically the first time the app
starts (the background scheduler runs immediately on boot, not just once
a day). If you ever need to force it manually - e.g. after clearing the
database - call:

```
POST https://aljex-listener.onrender.com/ms-graph/subscribe
```

with the same username/password Basic Auth used by the rest of this app's
admin routes (`SYNC_USERNAME` / `SYNC_PASSWORD`).

## Checking it's healthy

```
GET https://aljex-listener.onrender.com/health/graph-subscription
```

(same Basic Auth) shows the current subscription ID, its expiration, and
whether the last renewal attempt succeeded.

## What happens if this breaks

If the Entra credentials stop working, or a daily renewal is ever missed
for more than ~36 hours, `ensure_subscription_fresh()` logs a loud ERROR
in the Render logs (search for `GRAPH SUBSCRIPTION RENEWAL FAILED`).

That's log-only by default. To also get proactively alerted (not just
"the log has an error in it somewhere"), set `HEALTHCHECK_PING_URL` to a
free [healthchecks.io](https://healthchecks.io) check URL (or similar
dead-man's-switch service): this app pings it after every successful
renewal, and pings a `/fail` suffix on failure. If you don't hear from it
when you should, the service emails you - this works even if the app
crashes outright, since it doesn't depend on the failure path itself
running successfully. This isn't set up yet; it's optional.

Either way, the existing hourly schedule for the "Carrier Auto-Respond"
routine keeps running independently of this webhook. If the real-time
path goes dark, carrier emails still get picked up within the hour - they
just stop being near-instant until this is fixed.
