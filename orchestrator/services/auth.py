"""
Auth Service — White Portal First-Boot Security
================================================
Manages admin account creation and credential verification.

First-Boot Protocol
-------------------
If admin_accounts is empty → is_first_boot() returns True.
The AuthGuardMiddleware in main.py redirects ALL /web/ requests to
/web/setup until an admin account is created.  Once any account exists,
normal login/session flow applies.

Password Security
-----------------
Uses passlib[bcrypt] — bcrypt with automatic salting and work-factor 12.
Passwords are never stored or logged in plaintext.
"""

from __future__ import annotations

import logging

from passlib.context import CryptContext

logger = logging.getLogger(__name__)

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    def __init__(self, pool) -> None:
        self._pool = pool

    # ── First-Boot Check ──────────────────────────────────────────────────────

    async def is_first_boot(self) -> bool:
        """True if no admin accounts exist yet (first launch)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt FROM admin_accounts"
            )
            return int(row["cnt"]) == 0

    # ── Account Management ────────────────────────────────────────────────────

    async def create_admin(self, username: str, password: str) -> bool:
        """
        Hash *password* and persist a new admin account.
        Returns False if the username is already taken.
        """
        hashed = _pwd_ctx.hash(password)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO admin_accounts (username, password_hash) VALUES ($1, $2)",
                    username.strip().lower(),
                    hashed,
                )
            logger.info("Admin account created: %s", username.strip().lower())
            return True
        except Exception:
            # asyncpg raises UniqueViolationError on duplicate username
            return False

    # ── Credential Verification ───────────────────────────────────────────────

    async def verify(self, username: str, password: str) -> bool:
        """Verify credentials. Returns True on successful match."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT password_hash FROM admin_accounts WHERE username = $1",
                username.strip().lower(),
            )
        if not row:
            # Run a dummy verify to keep timing constant (prevent user enumeration)
            _pwd_ctx.dummy_verify()
            return False
        return _pwd_ctx.verify(password, row["password_hash"])
