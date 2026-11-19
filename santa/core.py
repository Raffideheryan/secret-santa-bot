"""Pure crypto, pairing, and date helpers (no Telegram or database coupling)."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Vault:
    """Authenticated encryption. Losing or rotating this key makes old rows unreadable."""

    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.urlsafe_b64decode(encoded_key.strip().encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("ENCRYPTION_KEY must be URL-safe base64") from exc
        if len(key) != 32:
            raise ValueError("ENCRYPTION_KEY must encode exactly 32 random bytes")
        self._cipher = AESGCM(key)

    def seal(self, text: str, context: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(
            nonce, text.encode("utf-8"), context.encode("utf-8")
        )

    def open(self, data: bytes, context: str) -> str:
        if len(data) < 29:
            raise ValueError("Invalid encrypted data")
        return self._cipher.decrypt(
            data[:12], data[12:], context.encode("utf-8")
        ).decode("utf-8")


def new_encryption_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def new_invite_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def secure_cycle(ids: list[int]) -> dict[int, int]:
    """One random directed cycle: no self-assignments or reciprocal pairs for n>=3."""
    if len(ids) < 3 or len(set(ids)) != len(ids):
        raise ValueError("At least three unique participants required")
    shuffled = ids.copy()
    secrets.SystemRandom().shuffle(shuffled)
    return {shuffled[i]: shuffled[(i + 1) % len(shuffled)] for i in range(len(shuffled))}


def local_datetime_to_utc(value: str, tz_name: str) -> datetime:
    """Read explicit YYYY-MM-DD HH:MM in an IANA timezone."""
    local = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    zone = ZoneInfo(tz_name)
    aware = local.replace(tzinfo=zone)
    # Reject nonexistent local times at a daylight-saving transition.
    if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
        raise ValueError("This local time does not exist in the selected timezone")
    return aware.astimezone(timezone.utc)
