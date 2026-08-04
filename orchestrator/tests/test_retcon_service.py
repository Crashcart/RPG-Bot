"""
Unit tests for orchestrator/services/retcon.py — RetconService.

All asyncpg pool calls are mocked; no live database is required.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.payloads import RetconRequest, RetconResponse
from orchestrator.services.retcon import RetconService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool(row: Any = None) -> MagicMock:
    """Return a fake asyncpg connection pool."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=row)
    pool.execute  = AsyncMock()

    # Context-manager support for acquire()
    conn = MagicMock()
    conn.execute  = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncCtxMgr())
    pool.acquire  = MagicMock(return_value=_AsyncCtxMgr(conn))

    return pool


class _AsyncCtxMgr:
    """Minimal async context manager that yields a fixed value."""

    def __init__(self, value: Any = None) -> None:
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_):
        return False


VALID_INTENT_ID = str(uuid.uuid4())
ADMIN_ID = "admin-snowflake-123"

_GOOD_DELTA = {
    "pre_state":  {"hp": 25, "max_hp": 30, "gold": 10},
    "post_state": {"hp": 10, "max_hp": 30, "gold": 10},
}


def _row(*, retconned: bool = False, character_id: Any = uuid.uuid4(), state_delta: Any = None):
    return {
        "id":           uuid.uuid4(),
        "character_id": character_id,
        "state_delta":  state_delta if state_delta is not None else _GOOD_DELTA,
        "retconned":    retconned,
    }


def _make_req(reason: str = "test retcon", **kwargs) -> RetconRequest:
    return RetconRequest(
        intent_id=VALID_INTENT_ID,
        admin_id=ADMIN_ID,
        reason=reason,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

class TestRetconErrorPaths:

    @pytest.mark.asyncio
    async def test_raises_on_missing_intent_id(self):
        pool = _make_pool(row=None)
        svc  = RetconService(pool)

        with pytest.raises(ValueError, match="No action found"):
            await svc.apply_retcon(_make_req())

    @pytest.mark.asyncio
    async def test_raises_if_already_retconned(self):
        pool = _make_pool(row=_row(retconned=True))
        svc  = RetconService(pool)

        with pytest.raises(ValueError, match="already been retconned"):
            await svc.apply_retcon(_make_req())

    @pytest.mark.asyncio
    async def test_raises_if_no_character_id(self):
        pool = _make_pool(row=_row(character_id=None))
        svc  = RetconService(pool)

        with pytest.raises(ValueError, match="no character_id"):
            await svc.apply_retcon(_make_req())

    @pytest.mark.asyncio
    async def test_raises_if_pre_state_missing(self):
        bad_delta = {"pre_state": {}, "post_state": {"hp": 10}}
        pool = _make_pool(row=_row(state_delta=bad_delta))
        svc  = RetconService(pool)

        with pytest.raises(ValueError, match="No pre_state found"):
            await svc.apply_retcon(_make_req())

    @pytest.mark.asyncio
    async def test_raises_if_pre_state_key_absent(self):
        bad_delta = {"post_state": {"hp": 10}}
        pool = _make_pool(row=_row(state_delta=bad_delta))
        svc  = RetconService(pool)

        with pytest.raises(ValueError, match="No pre_state found"):
            await svc.apply_retcon(_make_req())


# ---------------------------------------------------------------------------
# Happy path — state_delta as dict (asyncpg JSONB native)
# ---------------------------------------------------------------------------

class TestRetconHappyPathDict:

    @pytest.mark.asyncio
    async def test_returns_retcon_response(self):
        char_id = uuid.uuid4()
        pool    = _make_pool(row=_row(character_id=char_id))
        svc     = RetconService(pool)

        result = await svc.apply_retcon(_make_req())

        assert isinstance(result, RetconResponse)
        assert result.intent_id     == VALID_INTENT_ID
        assert result.character_id  == str(char_id)
        assert result.restored_stats == _GOOD_DELTA["pre_state"]

    @pytest.mark.asyncio
    async def test_transaction_executes_three_statements(self):
        pool = _make_pool(row=_row())
        svc  = RetconService(pool)
        await svc.apply_retcon(_make_req())

        conn = pool.acquire.return_value._value
        assert conn.execute.call_count == 3, (
            "Expected UPDATE characters, UPDATE action_log, INSERT retcon_log"
        )

    @pytest.mark.asyncio
    async def test_update_characters_uses_pre_state(self):
        pool = _make_pool(row=_row())
        svc  = RetconService(pool)
        await svc.apply_retcon(_make_req())

        conn      = pool.acquire.return_value._value
        first_sql = conn.execute.call_args_list[0][0][0]
        first_arg = conn.execute.call_args_list[0][0][1]
        assert "UPDATE characters" in first_sql
        assert json.loads(first_arg) == _GOOD_DELTA["pre_state"]

    @pytest.mark.asyncio
    async def test_action_log_flagged_retconned(self):
        pool = _make_pool(row=_row())
        svc  = RetconService(pool)
        await svc.apply_retcon(_make_req())

        conn      = pool.acquire.return_value._value
        second_sql = conn.execute.call_args_list[1][0][0]
        assert "retconned" in second_sql.lower()
        assert "action_log" in second_sql.lower()

    @pytest.mark.asyncio
    async def test_retcon_log_audit_insert(self):
        pool = _make_pool(row=_row())
        svc  = RetconService(pool)
        await svc.apply_retcon(_make_req("hallucination"))

        conn      = pool.acquire.return_value._value
        third_sql = conn.execute.call_args_list[2][0][0]
        assert "retcon_log" in third_sql.lower()
        assert "INSERT" in third_sql.upper()

    @pytest.mark.asyncio
    async def test_admin_id_passed_to_retcon_log(self):
        pool = _make_pool(row=_row())
        svc  = RetconService(pool)
        await svc.apply_retcon(_make_req())

        conn      = pool.acquire.return_value._value
        call_args = conn.execute.call_args_list[2][0]
        # admin_id is the 3rd positional arg after the SQL string
        assert ADMIN_ID in call_args


# ---------------------------------------------------------------------------
# Happy path — state_delta as JSON string
# ---------------------------------------------------------------------------

class TestRetconStateDeltaAsString:

    @pytest.mark.asyncio
    async def test_parses_json_string_delta(self):
        pool = _make_pool(row=_row(state_delta=json.dumps(_GOOD_DELTA)))
        svc  = RetconService(pool)

        result = await svc.apply_retcon(_make_req())
        assert result.restored_stats == _GOOD_DELTA["pre_state"]


# ---------------------------------------------------------------------------
# Intent_id validation
# ---------------------------------------------------------------------------

class TestIntentIdHandling:

    @pytest.mark.asyncio
    async def test_invalid_uuid_raises_value_error(self):
        pool = _make_pool(row=None)
        svc  = RetconService(pool)

        with pytest.raises((ValueError, Exception)):
            await svc.apply_retcon(RetconRequest(
                intent_id="not-a-uuid",
                admin_id=ADMIN_ID,
                reason="",
            ))
