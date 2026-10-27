# Telegram delivery

Render free web services stop after an idle period. Outbound `getUpdates`
polling cannot wake a stopped web service. A self-ping running in that same
process cannot run while the service is asleep. The incident on 2026-10-27
was confirmed when opening the web URL woke a new instance and the pending
TikTok URLs immediately appeared in its logs.

## Production

On Render, `python main.py` now selects HTTPS webhook delivery automatically
using `RENDER_EXTERNAL_URL` (or `SELF_URL`). Telegram's incoming POST wakes the
web service. The first request after inactivity can include Render's cold-start
delay; this does not require opening the website manually.

The endpoint is `/telegram/webhook`. It validates Telegram's secret header
using a stable HMAC derived from the existing bot token. The secret and bot
token are never placed in the URL or logs. Startup registers and verifies the
webhook without dropping pending updates. Shutdown deliberately leaves it
registered so future messages can wake a new instance.

`TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` are required on Render. The existing
Turso database stores a separate `telegram_inbox` table. This path never falls
back to ephemeral local storage: failed or uncertain writes return HTTP 503
so Telegram can retry. Local development can use SQLite.

Updates are acknowledged only after storage commits. Update IDs suppress
duplicate delivery. Two workers use atomic SQL leases so concurrent or
overlapping instances cannot claim the same active update. Payloads are
cleared after completion and deduplication records are removed after 24 hours.
Pending work and expired leases are recovered from the same database after a
restart. Inbox capacity is bounded to 200 pending/running updates; overload
returns 503 rather than accepting work that cannot be retained.

A handler exception does not stop reception. A hanging job is detected after
12 minutes, and a database call stuck for over a minute triggers process exit
for host recovery. A crashed running job becomes available when its 15-minute
lease expires. Download and Telegram network timeouts remain bounded. These
mechanisms add no paid service or dependency.

`GET /healthz` reports delivery readiness and persistent storage mode without
exposing user data or credentials. Startup logs include `Telegram webhook
verified persistent_inbox=True`. Processing logs record only update IDs and
outcomes for the inbox.

## Limits

Telegram delivery retries are deduplicated by update ID. A user deliberately
sending the same URL in two different messages creates two different requests.
Exactly-once external sending cannot be guaranteed if a process dies after
Telegram accepts an upload but before completion is recorded. A completed
handler's storage write is retried without replaying the handler to reduce that
window. Provider outages, hosting quotas and upstream restrictions can still
delay or prevent downloads.

## Validation and rollback

Run `python -m unittest -v test_reliable_delivery test_media_support test_signatures`.
The delivery tests cover authenticated receipt, commit-before-ack, duplicate
delivery, database failure, capacity, restart recovery, atomic claiming,
independent workers, handler exceptions, completion write retries, watchdog
recovery, and persistent remote-storage behavior. Existing media and signature
tests protect photos, audio and the animated X-path signature.

To intentionally return to polling, deploy this version with
`BOT_TRANSPORT=polling`; startup removes the webhook with
`drop_pending_updates=False` before polling. Reverting only the source to an
older polling-only commit leaves the webhook registered and will cause a 409
conflict; remove it without dropping updates before using such a version.

Official references:
- https://render.com/docs/free
- https://core.telegram.org/bots/api#setwebhook

## Availability checks

The inbox uses stateless Turso HTTP pipeline requests with bounded timeouts.
GitHub Actions schedules an external readiness request every five minutes;
scheduled runs can be delayed and are not an uptime guarantee. The health
endpoint checks both workers, overdue work, and an actual inbox database read.
It returns 503 when those checks fail, without restarting or changing jobs.
The startup public probe is a separate receipt/storage/dispatch check. During
rolling deployments its public request can be served by the outgoing instance;
it is not proof of a real user receiving a video on the new instance.
