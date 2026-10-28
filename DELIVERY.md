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

## Five-minute heartbeat and recovery

`bot_heartbeat.py` replaces the old one-shot public probe, maintenance watchdog,
and polling self-ping. It runs every 300 seconds on a monotonic clock without
starting overlapping checks. Each cycle checks Telegram's registered webhook,
repairs a missing or changed registration or a new delivery error without
clearing pending updates, then submits a no-message update through this
instance's localhost HTTP endpoint and verifies durable worker completion.
It prunes completed records and atomically rewrites
`/tmp/the966bot-heartbeat.json` with the timestamp, cycle count and outcome.
This runtime file is ephemeral, contains no credentials, and is recreated on
startup. The code itself is versioned in `bot_heartbeat.py`.

An independent supervisor checks every 15 seconds. A database call stuck for
60 seconds, a dead worker, a handler stuck for 12 minutes, a heartbeat cycle
stuck for 150 seconds, or no successful cycle for 660 seconds exits the process
for hosting recovery. An isolated failed check is retried at the next cycle;
it does not interrupt active downloads. Existing durable leases survive
restarts, and payloads remain in Turso until processing completes.

`GET /healthz` returns cached heartbeat age, failures, worker readiness, instance
identity and deployed revision without network calls or user data. Configure
Render's Health Check Path to `/healthz` so host supervision observes delivery
readiness instead of only an open TCP socket. HTTP serving uses Waitress.

## Limits

Telegram delivery retries are deduplicated by update ID. A user deliberately
sending the same URL in two different messages creates two different requests.
Exactly-once external sending cannot be guaranteed if a process dies after
Telegram accepts an upload but before completion is recorded. A completed
handler's storage write is retried without replaying the handler to reduce that
window. Provider outages, hosting quotas and upstream restrictions can still
delay or prevent downloads.

## Validation and rollback

Run `python -m unittest -v test_bot_heartbeat test_reliable_delivery test_media_support test_signatures`.
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

## Hosting limitation and observed incident

On 2026-10-28 the service was confirmed on Render's Free compute plan with an
empty HTTP health-check setting. Logs showed successful startup around 06:22
UTC followed by graceful shutdown at 06:37 UTC, and another startup around
13:20 UTC followed by shutdown at 13:35 UTC. These intervals match the free
plan's documented idle spin-down. External availability probes appeared hours
apart despite the five-minute GitHub Actions schedule.

By default each successful internal cycle also makes a best-effort public
health request (`BOT_PUBLIC_HEARTBEAT=true`). This can reduce idle spin-down,
but a process that has already stopped cannot ping or repair itself. The
existing GitHub Actions external schedule remains a secondary wake-up check,
not an exact timer: GitHub can delay or drop scheduled jobs and disables them
in inactive public repositories. Neither mechanism guarantees always-on service.

To remove idle suspension at its source, this Render service needs an always-on
paid compute plan, or migration to another always-on host. No paid plan is
activated by these files. On an always-on instance the public self-request can
be disabled with `BOT_PUBLIC_HEARTBEAT=false`; internal checks and recovery stay
active. Hosting outages, quotas and failures outside the process remain possible.

References:
- https://render.com/docs/free
- https://render.com/docs/health-checks
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
