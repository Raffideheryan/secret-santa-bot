"""Secret Santa: transactionally checkpointed Telegram polling + durable outbox."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from santa.core import Vault, local_datetime_to_utc, new_invite_token, token_digest
from santa.storage import (
    close_event, draw_event, enqueue, ensure_schema, find_pair, find_santa,
    latest_event, participant_by_telegram,
)

LOG = logging.getLogger("santa")
HELP = (
    "🎄 Secret Santa\n"
    "/start — your registration and event status\n"
    "/recipient — see your recipient and their wishlist\n"
    "/wishlist YOUR IDEAS — save an encrypted wishlist\n"
    "/to_recipient — send your next texts to your assigned recipient\n"
    "/to_santa — reply anonymously to your own Santa\n"
    "/help — show commands\n\n"
    "Only private text messages are supported, to avoid leaking photo metadata. "
    "Do not include your name or identifying details in anonymous messages."
)
ADMIN_HELP = (
    "\n\nOrganizer commands:\n"
    "/create TITLE | YYYY-MM-DD HH:MM | YYYY-MM-DD HH:MM | YYYY-MM-DD HH:MM\n"
    "Times: draw | gift exchange | close (in configured timezone)\n"
    "/add Anna, David, Maria (or one name per line)\n"
    "/invites — regenerate the pending invite-link list (without rotating tokens)\n"
    "/status — who has joined, without revealing pairs\n"
    "/remove NAME — remove a participant before the draw\n"
    "/draw — perform the one-time draw now if everyone has joined\n"
    "/health — show delivery queue statistics\n"
    "/retry_failed — retry blocked notifications after issues are fixed"
)
MAX_PEOPLE = 50


def chunks(lines: list[str], limit: int = 3500) -> list[str]:
    result: list[str] = []
    part = ""
    for line in lines:
        if len(line) > limit:
            raise ValueError("Line exceeds message limit")
        if part and len(part) + len(line) + 1 > limit:
            result.append(part)
            part = ""
        part += ("\n" if part else "") + line
    if part:
        result.append(part)
    return result


class Replies:
    """Responses are queued inside the same transaction as the input checkpoint."""

    def __init__(self, con: asyncpg.Connection, vault: Vault, update_id: int,
                 chat_id: int, event_id: int | None):
        self.con, self.vault, self.update_id = con, vault, update_id
        self.chat_id, self.event_id = chat_id, event_id
        self.counter = 0

    async def say(self, body: str):
        self.counter += 1
        await enqueue(
            self.con, self.vault, self.chat_id, body,
            f"update:{self.update_id}:{self.counter}", self.event_id,
        )

    async def say_lines(self, lines: list[str]):
        for block in chunks(lines):
            await self.say(block)


async def route_message(con: asyncpg.Connection, vault: Vault, admins: set[int],
                        bot_username: str, configured_tz: str, update) -> None:
    msg = update.message
    if not msg or not msg.from_user:
        return
    chat_id = msg.chat.id
    tg_id = msg.from_user.id
    if msg.chat.type != "private":
        # Don't echo sensitive content into groups.
        return
    raw = msg.text or ""
    parts = raw.split(maxsplit=1)
    first = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    cmd = first.split("@")[0].lower() if first.startswith("/") else ""
    args = rest.strip()
    event = await con.fetchrow("SELECT * FROM events ORDER BY id DESC LIMIT 1 FOR UPDATE")
    event_id = event["id"] if event else None
    reply = Replies(con, vault, update.update_id, chat_id, event_id)
    is_admin = tg_id in admins

    if cmd == "/help":
        await reply.say(HELP + (ADMIN_HELP if is_admin else ""))
        return

    if cmd == "/create":
        if not is_admin:
            await reply.say("Only the organizer can create an event.")
            return
        if event and event["status"] != "closed":
            await reply.say("An event is still active. Complete it before creating another.")
            return
        fields = [x.strip() for x in args.split("|")]
        if len(fields) != 4 or not (1 <= len(fields[0]) <= 80):
            await reply.say(
                "Usage:\n/create Christmas 2026 | 2026-12-01 20:00 | "
                "2026-12-25 18:00 | 2026-12-26 18:00"
            )
            return
        try:
            draw_at, exchange_at, close_at = [
                local_datetime_to_utc(x, configured_tz) for x in fields[1:]
            ]
        except (ValueError, KeyError):
            await reply.say("Invalid date. Use YYYY-MM-DD HH:MM in " + configured_tz)
            return
        if not (datetime.now(timezone.utc) < draw_at < exchange_at < close_at):
            await reply.say("Draw must be in the future, before exchange and close.")
            return
        new_id = await con.fetchval(
            """INSERT INTO events(title,timezone,draw_at,exchange_at,close_at)
               VALUES ($1,$2,$3,$4,$5) RETURNING id""",
            fields[0], configured_tz, draw_at, exchange_at, close_at,
        )
        reply.event_id = new_id
        await reply.say(
            f"✅ Created: {fields[0]}\n"
            f"Timezone: {configured_tz}\n"
            f"Draw: {fields[1]}\nExchange: {fields[2]}\nClose: {fields[3]}\n\n"
            "Add participants with /add Anna, David, Maria. "
            "The organizer never receives an assignment list."
        )
        return

    if cmd == "/start" and args:
        if not event or event["status"] != "registration":
            await reply.say("This invitation is no longer active.")
            return
        if len(args) > 128:
            await reply.say("Invalid invitation.")
            return
        row = await con.fetchrow(
            """SELECT * FROM participants
               WHERE event_id=$1 AND invite_hash=$2 FOR UPDATE""",
            event_id, token_digest(args),
        )
        if row is None or row["telegram_id"] is not None:
            await reply.say("Invalid or already used invitation. Ask your organizer for a link.")
            return
        existing = await participant_by_telegram(con, event_id, tg_id)
        if existing:
            await reply.say(f"You are already registered as {existing['name']}.")
            return
        await con.execute(
            """UPDATE participants
               SET telegram_id=$1,joined_at=now(),invite_hash=NULL,invite_enc=NULL
               WHERE id=$2""", tg_id, row["id"],
        )
        reply.event_id = event_id
        await reply.say(
            f"✅ Welcome, {row['name']}!\n"
            f"You joined {event['title']}. The private draw is scheduled automatically.\n\n"
            + HELP
        )
        return

    if cmd == "/start":
        person = (
            await participant_by_telegram(con, event_id, tg_id)
            if event and event["status"] != "closed" else None
        )
        if person:
            await reply.say(
                f"🎄 Registered as {person['name']} for {event['title']}.\n"
                + ("The draw has not happened yet.\n" if event["status"] == "registration"
                   else "Assignments are ready! Use /recipient.\n")
                + HELP
            )
        elif is_admin:
            await reply.say(HELP + ADMIN_HELP)
        else:
            await reply.say("Welcome! Request your personal invitation link from the organizer.\n" + HELP)
        return

    if cmd in ("/add", "/invites", "/status", "/remove", "/draw", "/health", "/retry_failed"):
        if not is_admin:
            await reply.say("Only the organizer can use this command.")
            return
        if not event or event["status"] == "closed":
            await reply.say("No active event. Use /create first.")
            return

        if cmd == "/health":
            counts = await con.fetch(
                """SELECT state, count(*) AS total FROM outbox
                   WHERE event_id=$1 GROUP BY state""", event_id,
            )
            metrics = {row["state"]: row["total"] for row in counts}
            oldest = await con.fetchval(
                """SELECT min(created_at) FROM outbox
                   WHERE event_id=$1 AND state='pending'""", event_id,
            )
            age = (
                str(int((datetime.now(timezone.utc) - oldest).total_seconds()))
                + " seconds" if oldest else "none"
            )
            await reply.say(
                f"Health for {event['title']}:\n"
                f"Pending deliveries: {metrics.get('pending', 0)}\n"
                f"Blocked deliveries: {metrics.get('blocked', 0)}\n"
                f"Oldest pending: {age}\n"
                f"Draw status: {event['status']}\n"
                "No sender/recipient relationships are shown."
            )
            return

        if cmd == "/retry_failed":
            result = await con.execute(
                """UPDATE outbox SET state='pending',locked_until=NULL,
                   next_attempt_at=now() WHERE event_id=$1 AND state='blocked'""",
                event_id,
            )
            await reply.say(f"✅ Re-queued {result.split()[-1]} blocked messages.")
            return

        if cmd == "/status":
            people = await con.fetch(
                "SELECT name,telegram_id FROM participants WHERE event_id=$1 ORDER BY id", event_id
            )
            registered = sum(p["telegram_id"] is not None for p in people)
            lines = [
                f"🎄 {event['title']} — {event['status']}",
                f"Registered: {registered}/{len(people)}",
                f"Draw: {event['draw_at'].astimezone(ZoneInfo(event['timezone'])).strftime('%Y-%m-%d %H:%M')} ({event['timezone']})",
                "Pair identities are NEVER shown here.",
            ]
            lines += [
                ("✅ " if p["telegram_id"] is not None else "⏳ ") + p["name"]
                for p in people
            ]
            await reply.say_lines(lines)
            return

        if cmd == "/draw":
            result = await draw_event(con, vault, event_id)
            await reply.say(result)
            return

        if event["status"] != "registration":
            await reply.say("Participant list is locked permanently after the draw.")
            return

        if cmd == "/add":
            names = [n.strip() for n in args.replace(",", "\n").splitlines() if n.strip()]
            if not names:
                await reply.say("Usage: /add Anna, David, Maria\nOr /add followed by one name per line.")
                return
            existing = await con.fetch(
                "SELECT name FROM participants WHERE event_id=$1", event_id
            )
            seen = {p["name"].casefold() for p in existing}
            if len(existing) + len(names) > MAX_PEOPLE:
                await reply.say(f"Maximum {MAX_PEOPLE} participants per event.")
                return
            if (any(len(n) > 50 or len(n) < 1 or any(ord(c) < 32 for c in n) for n in names)
                or len({n.casefold() for n in names}) != len(names)
                or any(n.casefold() in seen for n in names)):
                await reply.say("Duplicate/invalid name. Use unique names under 51 characters.")
                return
            links = ["✅ Created personal invitations (send each link privately):"]
            for name in names:
                token = new_invite_token()
                await con.execute(
                    """INSERT INTO participants(event_id,name,invite_hash,invite_enc)
                       VALUES($1,$2,$3,$4)""",
                    event_id, name, token_digest(token),
                    vault.seal(token, f"invite:{event_id}:{name}"),
                )
                links.append(f"{name}: https://t.me/{bot_username}?start={token}")
            await reply.say_lines(links)
            return

        if cmd == "/invites":
            people = await con.fetch(
                """SELECT name,invite_enc FROM participants
                   WHERE event_id=$1 AND telegram_id IS NULL ORDER BY id""", event_id
            )
            lines = ["⏳ Pending invitations (one-time links):"]
            for person in people:
                if person["invite_enc"] is not None:
                    token = vault.open(
                        person["invite_enc"], f"invite:{event_id}:{person['name']}"
                    )
                    lines.append(
                        f"{person['name']}: https://t.me/{bot_username}?start={token}"
                    )
            await reply.say_lines(lines if len(lines) > 1 else ["No pending invitations."])
            return

        if cmd == "/remove":
            row = await con.fetchrow(
                """SELECT * FROM participants WHERE event_id=$1 AND name=$2 FOR UPDATE""",
                event_id, args,
            )
            if not row:
                await reply.say("Name not found. Use /status to see the exact spelling.")
                return
            await con.execute("DELETE FROM participants WHERE id=$1", row["id"])
            await reply.say(f"Removed {row['name']} before the draw.")
            if row["telegram_id"] is not None:
                await enqueue(
                    con, vault, row["telegram_id"],
                    "Your registration was cancelled by the event organizer.",
                    f"removed:{update.update_id}:{row['id']}", event_id,
                )
            return

    if not event or event["status"] == "closed":
        await reply.say("No active event. Ask the organizer for an invitation.")
        return

    person = await participant_by_telegram(con, event_id, tg_id)
    if person is None:
        await reply.say("You are not registered. Ask the organizer for your personal invite link.")
        return

    if cmd == "/wishlist":
        if not args:
            wishlist = (
                vault.open(person["wishlist_enc"], f"wishlist:{person['id']}")
                if person["wishlist_enc"] else "(not set)"
            )
            await reply.say("Your wishlist:\n" + wishlist + "\n\nUpdate with /wishlist YOUR IDEAS")
            return
        if len(args) > 1000:
            await reply.say("Wishlist is too long (maximum 1000 characters).")
            return
        await con.execute(
            "UPDATE participants SET wishlist_enc=$1 WHERE id=$2",
            vault.seal(args, f"wishlist:{person['id']}"), person["id"],
        )
        await reply.say("✅ Wishlist saved. Your Secret Santa can see it with /recipient.")
        return

    if event["status"] != "assigned":
        await reply.say("The draw has not happened yet. You can already set /wishlist.")
        return

    if cmd == "/recipient":
        recipient_id = await find_pair(con, vault, event_id, person["id"])
        recipient = await con.fetchrow("SELECT * FROM participants WHERE id=$1", recipient_id)
        wishlist = (
            vault.open(recipient["wishlist_enc"], f"wishlist:{recipient['id']}")
            if recipient["wishlist_enc"] else "(not set)"
        )
        await reply.say(
            f"🎁 Your recipient: {recipient['name']}\nWishlist: {wishlist}\n\n"
            "Use /to_recipient to start sending anonymous texts."
        )
        return

    if cmd in ("/to_recipient", "/to_santa"):
        mode = "recipient" if cmd == "/to_recipient" else "santa"
        await con.execute(
            "UPDATE participants SET mode=$1 WHERE id=$2", mode, person["id"]
        )
        await reply.say(
            "🎅 Recipient chat selected. Send your next text anonymously."
            if mode == "recipient" else
            "🎁 Chat with your own Santa selected. Send your next text anonymously."
        )
        return

    if cmd:
        await reply.say("Unknown command. Use /help.")
        return

    if not raw.strip():
        await reply.say("Only text is supported. Avoid pictures/files that might expose identity.")
        return
    if len(raw) > 1500:
        await reply.say("Anonymous messages are limited to 1500 characters.")
        return

    mode = person["mode"]
    target_id = (
        await find_pair(con, vault, event_id, person["id"])
        if mode == "recipient"
        else await find_santa(con, vault, event_id, person["id"])
    )
    target = await con.fetchrow(
        "SELECT telegram_id FROM participants WHERE event_id=$1 AND id=$2",
        event_id, target_id,
    )
    if target is None or target["telegram_id"] is None:
        await reply.say("Routing unavailable. Ask the organizer to check the event status.")
        return
    prefix = (
        "🎅 Anonymous message from your Secret Santa:\n\n"
        if mode == "recipient"
        else "🎁 Anonymous reply from your recipient:\n\n"
    )
    await enqueue(
        con, vault, target["telegram_id"], prefix + raw,
        f"relay:{update.update_id}", event_id,
    )
    await reply.say("✅ Anonymous message queued for delivery.")


async def process_update(pool: asyncpg.Pool, vault: Vault, admins: set[int],
                         username: str, tz_name: str, update) -> None:
    """Persist response(s), state changes and update offset in ONE DB transaction."""
    async with pool.acquire() as con:
        async with con.transaction():
            last = await con.fetchval(
                "SELECT last_update_id FROM poll_cursor WHERE singleton=TRUE FOR UPDATE"
            )
            if update.update_id <= last:
                return
            await route_message(con, vault, admins, username, tz_name, update)
            await con.execute(
                "UPDATE poll_cursor SET last_update_id=$1 WHERE singleton=TRUE",
                update.update_id,
            )


async def poll_forever(bot: Bot, pool: asyncpg.Pool, vault: Vault,
                       admins: set[int], username: str, tz_name: str) -> None:
    """One active poller, locked at PostgreSQL session level across restarts."""
    async with pool.acquire() as lock_con:
        got_lock = await lock_con.fetchval("SELECT pg_try_advisory_lock(73490071)")
        if not got_lock:
            raise RuntimeError("Another poller is active; do not run two bot instances")
        offset = (await lock_con.fetchval(
            "SELECT last_update_id FROM poll_cursor WHERE singleton=TRUE"
        )) + 1
        errors = 0
        while True:
            try:
                updates = await asyncio.wait_for(
                    bot.get_updates(offset=offset, timeout=20, limit=100,
                                    allowed_updates=["message"]),
                    timeout=45,
                )
                for update in updates:
                    await process_update(pool, vault, admins, username, tz_name, update)
                    offset = update.update_id + 1
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log Telegram contents, tokens, invite URLs or pair data.
                LOG.warning("polling error: %s", type(exc).__name__)
                errors += 1
                await asyncio.sleep(min(30, 2 ** min(errors, 5)))
                # Re-read committed checkpoint after unknown DB outcome.
                async with pool.acquire() as con:
                    offset = (await con.fetchval(
                        "SELECT last_update_id FROM poll_cursor WHERE singleton=TRUE"
                    )) + 1


async def deliver_forever(bot: Bot, pool: asyncpg.Pool, vault: Vault) -> None:
    while True:
        try:
            async with pool.acquire() as con:
                item = await con.fetchrow(
                    """UPDATE outbox SET locked_until=now()+interval '2 minutes',
                           attempts=attempts+1
                       WHERE id = (
                           SELECT id FROM outbox
                           WHERE state='pending' AND next_attempt_at<=now()
                             AND (locked_until IS NULL OR locked_until < now())
                           ORDER BY next_attempt_at,id LIMIT 1 FOR UPDATE SKIP LOCKED
                       )
                       RETURNING *"""
                )
            if item is None:
                await asyncio.sleep(0.6)
                continue
            try:
                text = vault.open(item["body_enc"], "outbox:" + item["dedupe_key"])
                await asyncio.wait_for(
                    bot.send_message(chat_id=item["chat_id"], text=text),
                    timeout=35,
                )
            except TelegramRetryAfter as exc:
                delay, code = max(2, int(exc.retry_after) + 1), "rate_limit"
            except (TelegramForbiddenError, TelegramBadRequest):
                async with pool.acquire() as con:
                    await con.execute(
                        """UPDATE outbox SET state='blocked',locked_until=NULL,
                           last_error_code='forbidden_or_bad_request' WHERE id=$1""",
                        item["id"],
                    )
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("delivery error: %s", type(exc).__name__)
                delay = min(3600, 2 ** min(item["attempts"], 11))
                code = type(exc).__name__[:60]
            else:
                async with pool.acquire() as con:
                    await con.execute(
                        """UPDATE outbox SET state='sent',sent_at=now(),
                           locked_until=NULL,body_enc=$2 WHERE id=$1""",
                        item["id"], b"",
                    )
                await asyncio.sleep(0.15)
                continue
            async with pool.acquire() as con:
                await con.execute(
                    """UPDATE outbox SET locked_until=NULL,
                       next_attempt_at=now()+($2::integer * interval '1 second'),
                       last_error_code=$3 WHERE id=$1""",
                    item["id"], int(delay), code,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("outbox worker error: %s", type(exc).__name__)
            await asyncio.sleep(3)


async def schedule_forever(pool: asyncpg.Pool, vault: Vault, admins: set[int]) -> None:
    while True:
        try:
            async with pool.acquire() as con:
                async with con.transaction():
                    event = await latest_event(con)
                    if event and event["status"] != "closed":
                        now = datetime.now(timezone.utc)
                        if now >= event["close_at"]:
                            await close_event(con, vault, event["id"])
                        elif event["status"] == "registration" and now >= event["draw_at"]:
                            outcome = await draw_event(con, vault, event["id"])
                            if outcome.startswith("At least") or outcome.startswith("Not everyone"):
                                for admin_id in admins:
                                    await enqueue(
                                        con, vault, admin_id,
                                        "⚠️ Scheduled draw is waiting: " + outcome
                                        + "\nUse /status, /invites or /remove to resolve it.",
                                        f"draw_wait:{event['id']}:{admin_id}", event["id"],
                                    )
                    # Safe automatic purge of already-delivered outbox records.
                    await con.execute(
                        "DELETE FROM outbox WHERE state IN ('sent','blocked') "
                        "AND created_at < now()-interval '7 days'"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("scheduler error: %s", type(exc).__name__)
        await asyncio.sleep(10)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ["BOT_TOKEN"]
    db_url = os.environ["DATABASE_URL"]
    vault = Vault(os.environ["ENCRYPTION_KEY"])
    admins = {int(x.strip()) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
    if not admins:
        raise RuntimeError("At least one ADMIN_IDS value required")
    tz_name = os.environ.get("EVENT_TIMEZONE", "Asia/Yerevan")
    local_datetime_to_utc("2030-01-01 12:00", tz_name)  # validate zone
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=6, command_timeout=40)
    try:
        await ensure_schema(pool)
        async with Bot(token=token) as bot:
            user = await bot.get_me()
            if not user.username:
                raise RuntimeError("The Telegram bot must have a username")
            LOG.info("Bot initialized and database ready")
            # Do not automatically discard Telegram's pending updates.
            await asyncio.gather(
                poll_forever(bot, pool, vault, admins, user.username, tz_name),
                deliver_forever(bot, pool, vault),
                schedule_forever(pool, vault, admins),
            )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
