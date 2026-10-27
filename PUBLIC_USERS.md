# Public Telegram user count

`/start` displays the number of unique known Telegram users, with a button
that fetches the current count. `/users` and `عدد المستخدمين` also show it.
Existing welcome messages are snapshots; the button reads the latest total.
Only the aggregate is published, without names or identifiers.

`telegram_users` uses the human Telegram user ID as its primary key. Repeat
downloads, changing usernames, retries, group chats and overlapping instances
cannot count the same ID twice. This is a cumulative count of known users,
not monthly active users or a count of people currently online. Bots and
anonymous/channel identities are excluded from new registrations.

At startup, known positive Telegram IDs from `usage_log` are imported using
`INSERT OR IGNORE`. Old records with no Telegram ID cannot be reconstructed
reliably from display names. Existing Telegram history and blocked users are
not enumerated through Telegram's API. The count therefore reflects recorded
users, with future interactions added automatically.

Production uses the existing Turso database through an independent bounded
connection and executor. It never switches to ephemeral storage on failure.
Ordinary download, audio and signature handlers enqueue observations without
waiting for SQL. A daemon processes the bounded queue and retries temporary
failures. `/start`, `/users` and the count button write the requesting user's
ID before reading the count. A failed read displays an unavailable message,
never a fabricated zero. No new dependency or paid resource is introduced.

Queued observations are best-effort across abrupt process termination; a
later interaction or known-ID download history imports the missing user.
Completed database registrations survive service restarts and deployments.

`PUBLIC_USERS_READY count=N` confirms initialization against the configured
database. Local tests cover duplicates, historical import, concurrent
instances, restart persistence, failure isolation, real Telegram dispatch,
the welcome/button UI and existing media handler routing.

Run `python -m unittest -v test_reliable_delivery test_media_support test_signatures test_public_users`.
