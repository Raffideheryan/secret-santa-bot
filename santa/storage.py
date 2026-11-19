"""PostgreSQL schema and atomic, restart-safe state transitions."""
from __future__ import annotations

import asyncpg
from datetime import datetime, timezone

from santa.core import Vault, secure_cycle

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    timezone TEXT NOT NULL,
    draw_at TIMESTAMPTZ NOT NULL,
    exchange_at TIMESTAMPTZ NOT NULL,
    close_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'registration'
        CHECK (status IN ('registration','assigned','closed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (draw_at < exchange_at AND exchange_at < close_at)
);
CREATE TABLE IF NOT EXISTS participants (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    telegram_id BIGINT,
    invite_hash TEXT UNIQUE,
    invite_enc BYTEA,
    wishlist_enc BYTEA,
    mode TEXT NOT NULL DEFAULT 'recipient'
        CHECK (mode IN ('recipient','santa')),
    joined_at TIMESTAMPTZ,
    UNIQUE (event_id, name),
    UNIQUE (event_id, telegram_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    giver_id BIGINT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    recipient_enc BYTEA NOT NULL,
    PRIMARY KEY (event_id, giver_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT REFERENCES events(id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    body_enc BYTEA NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','sent','blocked')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outbox_due_idx
  ON outbox(next_attempt_at, id) WHERE state='pending';
CREATE TABLE IF NOT EXISTS ingress_rates (
    telegram_id BIGINT PRIMARY KEY,
    window_started TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS poll_cursor (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    last_update_id BIGINT NOT NULL
);
INSERT INTO poll_cursor (singleton, last_update_id) VALUES (TRUE, -1)
ON CONFLICT (singleton) DO NOTHING;
"""


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as con:
        await con.execute(SCHEMA)


async def latest_event(con: asyncpg.Connection):
    return await con.fetchrow("SELECT * FROM events ORDER BY id DESC LIMIT 1")


async def participant_by_telegram(con: asyncpg.Connection, event_id: int, tg_id: int):
    return await con.fetchrow(
        "SELECT * FROM participants WHERE event_id=$1 AND telegram_id=$2",
        event_id, tg_id,
    )


async def enqueue(
    con: asyncpg.Connection, vault: Vault, chat_id: int, message: str,
    dedupe_key: str, event_id: int | None = None,
) -> None:
    if len(message) > 3900:
        raise ValueError("Outgoing Telegram message exceeds safe size limit")
    payload = vault.seal(message, "outbox:" + dedupe_key)
    await con.execute(
        """INSERT INTO outbox (event_id,chat_id,dedupe_key,body_enc)
           VALUES ($1,$2,$3,$4) ON CONFLICT (dedupe_key) DO NOTHING""",
        event_id, chat_id, dedupe_key, payload,
    )


async def draw_event(con: asyncpg.Connection, vault: Vault, event_id: int):
    """Call inside a transaction. Event row lock makes retries a no-op."""
    event = await con.fetchrow("SELECT * FROM events WHERE id=$1 FOR UPDATE", event_id)
    if event is None:
        return "Event not found."
    if event["status"] != "registration":
        return "Draw already completed or event closed; assignments never change."
    people = await con.fetch(
        "SELECT * FROM participants WHERE event_id=$1 ORDER BY id", event_id
    )
    if len(people) < 3:
        return "At least three participants are required."
    if any(p["telegram_id"] is None for p in people):
        return "Not everyone has registered yet. Existing assignments were not changed."
    pairs = secure_cycle([p["id"] for p in people])
    by_id = {p["id"]: p for p in people}
    for giver_id, recipient_id in pairs.items():
        payload = vault.seal(
            str(recipient_id), f"assignment:{event_id}:{giver_id}"
        )
        await con.execute(
            """INSERT INTO assignments (event_id,giver_id,recipient_enc)
               VALUES ($1,$2,$3)""", event_id, giver_id, payload,
        )
        giver, recipient = by_id[giver_id], by_id[recipient_id]
        note = (
            f"🎅 {event['title']}\n\n"
            f"You are Secret Santa for: {recipient['name']}!\n"
            "Your name stays hidden from your recipient.\n\n"
            "Use /to_recipient to write anonymously to them, "
            "and /to_santa to answer your own Santa. "
            "Use /recipient to view your assigned person's wishlist."
        )
        await enqueue(
            con, vault, giver["telegram_id"], note,
            f"assignment:{event_id}:{giver_id}", event_id,
        )
    await con.execute(
        "UPDATE events SET status='assigned' WHERE id=$1", event_id
    )
    return f"Draw completed for {len(people)} participants. Assignments are hidden."


async def close_event(con: asyncpg.Connection, vault: Vault, event_id: int) -> bool:
    """Erase all participants, assignments and queued content when an event closes."""
    event = await con.fetchrow("SELECT * FROM events WHERE id=$1 FOR UPDATE", event_id)
    if event is None or event["status"] == "closed":
        return False
    people = await con.fetch("SELECT telegram_id,id FROM participants WHERE event_id=$1", event_id)
    await con.execute("DELETE FROM outbox WHERE event_id=$1", event_id)
    await con.execute("DELETE FROM assignments WHERE event_id=$1", event_id)
    await con.execute("DELETE FROM participants WHERE event_id=$1", event_id)
    await con.execute("UPDATE events SET status='closed' WHERE id=$1", event_id)
    for person in people:
        if person["telegram_id"] is not None:
            await enqueue(
                con, vault, person["telegram_id"],
                f"🎄 {event['title']} is closed. Private assignments and wishlists have been deleted.",
                f"closure:{event_id}:{person['id']}", event_id,
            )
    return True


async def find_pair(
    con: asyncpg.Connection, vault: Vault, event_id: int, giver_id: int
) -> int | None:
    enc = await con.fetchval(
        "SELECT recipient_enc FROM assignments WHERE event_id=$1 AND giver_id=$2",
        event_id, giver_id,
    )
    if enc is None:
        return None
    return int(vault.open(enc, f"assignment:{event_id}:{giver_id}"))


async def find_santa(
    con: asyncpg.Connection, vault: Vault, event_id: int, recipient_id: int
) -> int | None:
    # Inverse index is intentionally NOT persisted in plaintext.
    rows = await con.fetch(
        "SELECT giver_id,recipient_enc FROM assignments WHERE event_id=$1", event_id
    )
    for row in rows:
        if int(vault.open(
            row["recipient_enc"], f"assignment:{event_id}:{row['giver_id']}"
        )) == recipient_id:
            return row["giver_id"]
    return None
