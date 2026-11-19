# Secret Santa — private, crash-resilient Telegram bot 🎄

One active event per deployment. Organizers manage **names, registrations and dates**; they **cannot see Santa–recipient assignments through the bot**. Every participant learns only their own assigned recipient and can exchange anonymous text messages in either direction.

**Status:** implementation, Docker Compose deployment, PostgreSQL persistence, tests, encrypted backups. Before your event, configure and run it, test the complete flow with a throwaway event, enable off-server encrypted backups, and monitor it. Code pushed to GitHub is **not** a running bot.

## 1. Requirements

- Linux VPS with Docker Engine + Docker Compose v2 (no public inbound port needed)
- Telegram bot from [@BotFather](https://t.me/BotFather); copy its token **privately**
- Numeric Telegram ID(s) of organizers; do **not** use Telegram usernames for access control
- A strong, unique 32-byte encryption key and PostgreSQL password
- Recommended: offsite backup destination, uptime monitoring, a separate private age key

Do not deploy the application with the example credentials. Keep the repo public but the **.env file private**.

## 2. Deploy

On the VPS:

~~~bash
git clone https://github.com/Raffideheryan/secret-santa-bot.git
cd secret-santa-bot
cp .env.example .env
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
~~~

Edit **.env** to set:

| Variable | Meaning |
|---|---|
| BOT_TOKEN | Telegram BotFather token |
| ADMIN_IDS | Comma-separated numeric Telegram IDs; only these may manage event |
| ENCRYPTION_KEY | Generated 32-byte URL-safe base64 key from command above |
| POSTGRES_PASSWORD | Unique, long PostgreSQL password |
| DATABASE_URL | Must have matching password in URL: postgresql://santa:YOUR_PASSWORD@db:5432/santa |
| EVENT_TIMEZONE | IANA zone for organizer commands, e.g. Asia/Yerevan |

If your database password contains reserved URL characters, percent-encode it in DATABASE_URL. Prefer a strong password without URL-reserved characters.

~~~bash
chmod 600 .env
docker compose up -d --build
docker compose ps
docker compose logs --tail=80 bot
~~~

Expected bot log: **Bot initialized and database ready**. Bot auto-creates its database tables on startup.

**Important:** only **one active bot poller** may use the token. If you used a webhook previously, disable the webhook first. If starting another instance, the PostgreSQL advisory lock prevents a second instance from consuming updates simultaneously.

## 3. Organizer flow: add names, start the process

All commands are sent to the bot in a **private chat**.

**A. Create an event.** Format: event title, planned draw, gift exchange, closure. Times use EVENT_TIMEZONE. Example for Yerevan:

~~~text
/create Christmas 2026 | 2026-12-01 20:00 | 2026-12-25 18:00 | 2026-12-26 18:00
~~~

Draw, exchange and close dates must be strictly increasing, with the draw still in the future.

**B. Add participants** (maximum 50 people, minimum 3 to draw):

~~~text
/add Anna, David, Maria, Arman, Sona
~~~

Or one name per line after /add. The organizer gets **one unique invitation link per name**, e.g.:

~~~text
Anna: https://t.me/YourBot?start=<secret-one-time-token>
David: https://t.me/YourBot?start=<different-secret-token>
~~~

Send each personalized URL privately to the intended person. Do not send the list to a public group. The bot stores token hashes for lookup, and encrypted token copies until they are used; an invitation becomes invalid once claimed.

**C. Check registration** with /status. It shows names and whether they've joined—**never pairings**. Use /invites to retrieve the still-pending links, or /remove ExactName to remove somebody **before** matching.

**D. Assign automatically** at the chosen draw date/time, **only when all listed people have joined**. If registration is incomplete, the bot alerts organizers and keeps trying. When the missing people join (or the organizer removes them before the draw), the bot safely completes it. Or use /draw earlier after all have joined. The draw is immutable and can't be repeated.

**E. Continue** until the close time. Anonymous conversations are possible after the draw. The bot closes automatically and destroys assignments, participant names/Telegram IDs, invitation records and wishlists at the close time. Telegram chat histories and external backups aren't purged by this operation.

After an event closes, organizers may /create another one. Only one event may be active at a time.

## 4. Participant flow

1. Click your unique invitation link and tap **Start**. The bot binds this name to your Telegram account. If the organizer accidentally sends the wrong link, ask them to remove and re-add you **before** the draw.
2. Optionally set a wishlist with /wishlist My favorite books, music and tea.
3. At draw time, receive a private message naming your recipient. **Only you learn your recipient**; the organizer gets a count.
4. /recipient shows recipient's name and current wishlist.
5. /to_recipient selects chat to your assigned person. Subsequent ordinary text messages go anonymously **to them**.
6. /to_santa selects chat to the person secretly assigned **to you**. Subsequent ordinary text messages go anonymously **to them**.
7. Switch between those modes any time. Use /start or /help for guidance.

The two chat directions are distinct. Text sent in either mode is relayed as **a fresh message from the bot**, never forwarded with sender attribution.

**Identity caution:** don't write your name, phone number, username, specific identifying details or identifying URLs inside anonymous text. Files, photos and voice recordings are intentionally not supported because they can leak metadata and identity.

## 5. Operations and reliability

- PostgreSQL records participant state, schedules, pair ciphertext, pending outbound messages and Telegram's **last completed update ID**.
- Inbound Telegram update processing and its cursor checkpoint commit in **one transaction**; a replay doesn't redo a successful command.
- Public bot messages are throttled to 30 updates per Telegram user per minute, with temporary counters regularly purged.
- The draw holds a database row lock and writes all assignments + notifications **in the same transaction**. A crash before commit rolls everything back; on restart the scheduled draw resumes from the durable state. A crash after commit **never redraws** the pairs.
- Outbound messages are encrypted in a durable outbox before the input update is marked complete. A worker retries temporary Telegram outages, with backoff and 429 retry-after handling.
- Docker Compose restart policy starts the bot after process failure and host reboot, assuming Docker itself is enabled.
- /health (organizer-only) shows pending/blocked delivery counts. If a participant blocked the bot, ask them to unblock/start it, then use /retry_failed.
- At event close, historical queued event messages are removed, all participant records and assignments are deleted, and closure notifications are queued. Sent-message contents are zeroed in the DB; old outbox metadata expires after seven days.

**Limitations you must understand:**

1. Telegram does not support idempotency keys for sendMessage. If it accepts a message and the process dies **before PostgreSQL marks it sent**, that one notification **may be delivered twice**. The draw and registration never repeat; message delivery is at-least-once, not provably exactly-once.
2. Telegram retains unacknowledged updates for **up to 24 hours**. An outage longer than that may lose incoming texts even though existing database records survive.
3. A bot restart does **not** protect against lost/destroyed VPS disks, lost encryption keys, Docker being disabled, long network outages, or Telegram account bans. Use encrypted, tested off-server backups and monitoring.
4. If a participant blocks the bot, delivery cannot succeed until they unblock it. /health and /retry_failed support recovery.

## 6. Encryption and anonymity boundaries

We use **AES-256-GCM** with randomly generated per-record nonces and context-bound authenticated data to encrypt: invitation tokens (until redeemed), all assignment recipient IDs, participant wishlists and queued outgoing message bodies. Link lookup uses SHA-256 of high-entropy random tokens.

Names, Telegram account IDs, event times, routing outbox destination chat IDs and delivery metadata remain **plaintext in PostgreSQL**. The organizer API contains no pair-lookup/export command. A compromised database **without the ENCRYPTION_KEY** cannot directly decrypt pair records, but can still expose these plaintext fields.

This is **not end-to-end encryption**. Telegram can process bot messages, and the running bot temporarily decrypts assignments to route text. **Anyone with full server/root access AND its runtime environment key, including an organizer who controls the VPS, can theoretically reconstruct assignments.** To prevent even the infrastructure owner from knowing pairs, one would need a separately trusted, access-restricted matching/relay service or stronger multiparty cryptographic design. Do not describe this setup as cryptographically organizer-proof against its own server owner.

Keep .env outside version control and backups encrypted. Never log message contents, pairing values or invitation tokens. The bot avoids logging these fields.

## 7. Encrypted backups + restore

A Docker volume alone is **not a backup**. Install [age](https://github.com/FiloSottile/age) **on the host**, create an age identity key **off the VPS**, keep its private identity offsite, and give the VPS only its **public** recipient string.

Example manual backup (run from the repo root):

~~~bash
export AGE_RECIPIENT='age1YOUR_PUBLIC_RECIPIENT_KEY'
bash scripts/backup.sh /var/backups/secret-santa
~~~

This generates encrypted PostgreSQL custom-format and encrypted .env backups. Both are required because the database records can't be read without ENCRYPTION_KEY. **Copy the encrypted files off the VPS immediately.** Automate this script daily with cron/systemd and an authenticated offsite copy, and test restores routinely. Never put the age private key into the VPS cron environment.

To restore on a **replacement VPS** after installing Docker and cloning the repository, copy the two encrypted backup files to the machine, then:

~~~bash
export AGE_IDENTITY_FILE=/secure/location/age-private-key.txt
bash scripts/restore.sh backup.pgcustom.age backup.env.age
# Inspect and apply the recovered .env.restored:
cp .env.restored .env
chmod 600 .env
bash scripts/restore.sh backup.pgcustom.age backup.env.age
~~~

Restore is destructive to the target event database. Do not run it against a healthy active event. The bot's committed Telegram update cursor is restored too, preventing unnecessary replay **within Telegram's update retention period**. A very old restored backup can cause missing updates or replay; test an incident recovery plan.

## 8. Development and tests

Create a disposable PostgreSQL database. **Do not point tests at your production database: tests TRUNCATE event tables.**

~~~bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://santa:santa_test@localhost:5432/santa_test
pytest -q
~~~

The included GitHub Actions workflow runs PostgreSQL integration tests, encryption/tamper tests, atomic rollback/retry tests, and checks the Compose config. Local bot runtime does not require opening an HTTP port.

## 9. Useful organizer commands

| Command | Function |
|---|---|
| /create ... | Event + draw/exchange/closure schedule |
| /add ... | Add names and produce individual invitations |
| /invites | View unclaimed invitation links |
| /status | See registration progress only |
| /remove Name | Remove person before draw |
| /draw | Start one-time draw early, if all joined |
| /health | Counts of pending and blocked Telegram messages |
| /retry_failed | Requeue previously blocked deliveries |

For privacy and operational safety, there is **no pair-viewing command, no secret-pair export, and no re-draw command**.
