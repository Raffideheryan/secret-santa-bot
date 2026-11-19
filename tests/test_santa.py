"""Unit and PostgreSQL recovery tests. CI provides a disposable Postgres service."""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest

from santa.core import (
    Vault, local_datetime_to_utc, new_encryption_key, new_invite_token,
    secure_cycle, token_digest,
)
from santa.__main__ import process_update
from santa.storage import (
    close_event, draw_event, ensure_schema, find_pair, find_santa,
)


def test_encryption_authentication_and_context():
    vault = Vault(new_encryption_key())
    cipher = vault.seal("Armenian անուն 🎁", "assignment:1:2")
    assert b"Armenian" not in cipher
    assert vault.open(cipher, "assignment:1:2") == "Armenian անուն 🎁"
    with pytest.raises(Exception):
        vault.open(cipher, "assignment:1:3")
    tampered = bytearray(cipher)
    tampered[-1] ^= 1
    with pytest.raises(Exception):
        vault.open(bytes(tampered), "assignment:1:2")
    with pytest.raises(ValueError):
        Vault("insecure")


def test_cryptographic_cycle_for_many_sizes():
    for size in range(3, 51):
        ids = list(range(100, size + 100))
        pairs = secure_cycle(ids)
        assert set(pairs) == set(ids)
        assert set(pairs.values()) == set(ids)
        assert all(g != r for g, r in pairs.items())
        assert all(pairs[r] != g for g, r in pairs.items())
        start, visited = ids[0], set()
        while start not in visited:
            visited.add(start)
            start = pairs[start]
        assert len(visited) == size
    with pytest.raises(ValueError):
        secure_cycle([1, 2])


def test_tokens_and_date():
    a, b = new_invite_token(), new_invite_token()
    assert a != b and len(a) > 35
    assert token_digest(a) != token_digest(b)
    assert local_datetime_to_utc("2027-01-01 12:00", "Asia/Yerevan").hour == 8


@pytest.fixture
def vault():
    return Vault(new_encryption_key())


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=1, max_size=3
    )
    await ensure_schema(pool)
    async with pool.acquire() as con:
        await con.execute("TRUNCATE TABLE events CASCADE")
        await con.execute("DELETE FROM outbox WHERE event_id IS NULL")
        await con.execute("UPDATE poll_cursor SET last_update_id=-1")
    yield pool
    await pool.close()


async def add_event(con):
    now = datetime.now(timezone.utc)
    return await con.fetchval(
        """INSERT INTO events(title,timezone,draw_at,exchange_at,close_at)
           VALUES ('Test','Asia/Yerevan',$1,$2,$3) RETURNING id""",
        now + timedelta(hours=1), now + timedelta(days=2),
        now + timedelta(days=3),
    )


@pytest.mark.asyncio
async def test_draw_rolls_back_and_then_survives_restart(pool, vault):
    async with pool.acquire() as con:
        event_id = await add_event(con)
        ids = []
        for i, name in enumerate(["Alice", "Bob", "Charlie", "Dina"]):
            ids.append(await con.fetchval(
                """INSERT INTO participants(event_id,name,telegram_id)
                   VALUES ($1,$2,$3) RETURNING id""", event_id, name, 1000 + i,
            ))
        with pytest.raises(RuntimeError):
            async with con.transaction():
                assert (await draw_event(con, vault, event_id)).startswith("Draw completed")
                raise RuntimeError("simulated power loss before transaction commit")
        assert await con.fetchval(
            "SELECT count(*) FROM assignments WHERE event_id=$1", event_id
        ) == 0
        assert await con.fetchval(
            "SELECT status FROM events WHERE id=$1", event_id
        ) == "registration"

        async with con.transaction():
            assert (await draw_event(con, vault, event_id)).startswith("Draw completed")
        pair_a = {i: await find_pair(con, vault, event_id, i) for i in ids}
        assert set(pair_a.values()) == set(ids)
        for giver, recipient in pair_a.items():
            assert await find_santa(con, vault, event_id, recipient) == giver

        async with con.transaction():
            result = await draw_event(con, vault, event_id)
        assert "already completed" in result
        assert pair_a == {i: await find_pair(con, vault, event_id, i) for i in ids}
        assert await con.fetchval(
            "SELECT count(*) FROM outbox WHERE event_id=$1", event_id
        ) == len(ids)

        async with con.transaction():
            await close_event(con, vault, event_id)
        assert await con.fetchval(
            "SELECT count(*) FROM assignments WHERE event_id=$1", event_id
        ) == 0


def fake_update(update_id, chat_id, text):
    return SimpleNamespace(
        update_id=update_id,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="private"),
            from_user=SimpleNamespace(id=chat_id),
            text=text,
        ),
    )


@pytest.mark.asyncio
async def test_update_checkpoint_is_atomic_and_idempotent(pool, vault):
    date = (datetime.now(timezone.utc) + timedelta(days=3)).astimezone(
        __import__("zoneinfo").ZoneInfo("Asia/Yerevan")
    )
    draw = date.strftime("%Y-%m-%d %H:%M")
    exchange = (date + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    close = (date + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    create = fake_update(81, 100, f"/create Test | {draw} | {exchange} | {close}")
    await process_update(pool, vault, {100}, "SecretSantaTestBot",
                         "Asia/Yerevan", create)
    await process_update(pool, vault, {100}, "SecretSantaTestBot",
                         "Asia/Yerevan", create)
    async with pool.acquire() as con:
        assert await con.fetchval("SELECT count(*) FROM events") == 1
        assert await con.fetchval(
            "SELECT count(*) FROM outbox WHERE dedupe_key='update:81:1'"
        ) == 1
        assert await con.fetchval(
            "SELECT last_update_id FROM poll_cursor WHERE singleton=TRUE"
        ) == 81

    add = fake_update(82, 100, "/add Alice, Bob, Charlie")
    await process_update(pool, vault, {100}, "SecretSantaTestBot",
                         "Asia/Yerevan", add)
    async with pool.acquire() as con:
        assert await con.fetchval("SELECT count(*) FROM participants") == 3
        assert await con.fetchval(
            "SELECT count(*) FROM outbox WHERE dedupe_key='update:82:1'"
        ) == 1
    await process_update(pool, vault, {100}, "SecretSantaTestBot",
                         "Asia/Yerevan", add)
    async with pool.acquire() as con:
        assert await con.fetchval("SELECT count(*) FROM participants") == 3


@pytest.mark.asyncio
async def test_anonymous_relay_does_not_store_plaintext(pool, vault):
    async with pool.acquire() as con:
        event_id = await add_event(con)
        ids = []
        for i, name in enumerate(["Alice", "Bob", "Charlie"]):
            ids.append(await con.fetchval(
                """INSERT INTO participants(event_id,name,telegram_id)
                   VALUES ($1,$2,$3) RETURNING id""", event_id, name, 400 + i,
            ))
        async with con.transaction():
            await draw_event(con, vault, event_id)
        recipient_id = await find_pair(con, vault, event_id, ids[0])
        tg_target = await con.fetchval(
            "SELECT telegram_id FROM participants WHERE id=$1", recipient_id
        )
    await process_update(
        pool, vault, {999}, "SecretSantaTestBot", "Asia/Yerevan",
        fake_update(101, 400, "What gift would you like?"),
    )
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM outbox WHERE dedupe_key='relay:101'"
        )
        assert row["chat_id"] == tg_target
        assert b"What gift" not in row["body_enc"]
        assert "What gift" in vault.open(row["body_enc"], "outbox:relay:101")
