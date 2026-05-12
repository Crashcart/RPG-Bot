"""Unit tests for the Whisper Protocol (Issue #19)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.services.whisper_service import WhisperService


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_service(
    hidden_state: dict | None = None,
    redis_count: int = 1,
) -> tuple[WhisperService, MagicMock, MagicMock]:
    db    = MagicMock()
    redis = MagicMock()

    db.get_hidden_state       = AsyncMock(return_value=hidden_state or {})
    db.apply_hidden_state_delta = AsyncMock(return_value=hidden_state or {})
    db.log_whisper            = AsyncMock()

    redis.incr   = AsyncMock(return_value=redis_count)
    redis.expire = AsyncMock()

    svc = WhisperService(db=db, redis=redis)
    return svc, db, redis


# ── should_trigger_whisper ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_horror_action_type_triggers():
    svc, _, _ = _make_service()
    triggered, _ = await svc.should_trigger_whisper(
        character_id="char-1",
        action_type="sanity_check",
        reasoning="routine",
        outcome="success",
    )
    assert triggered is True


@pytest.mark.asyncio
async def test_horror_keyword_in_reasoning_triggers():
    svc, _, _ = _make_service()
    triggered, _ = await svc.should_trigger_whisper(
        character_id="char-1",
        action_type="melee_attack",
        reasoning="The eldritch horror loomed over the character",
        outcome="failure",
    )
    assert triggered is True


@pytest.mark.asyncio
async def test_low_sanity_triggers():
    svc, _, _ = _make_service(hidden_state={"sanity": 15, "flags": []})
    triggered, _ = await svc.should_trigger_whisper(
        character_id="char-1",
        action_type="melee_attack",
        reasoning="normal attack",
        outcome="success",
    )
    assert triggered is True


@pytest.mark.asyncio
async def test_paranoid_flag_triggers():
    svc, _, _ = _make_service(hidden_state={"sanity": 80, "flags": ["paranoid"]})
    triggered, _ = await svc.should_trigger_whisper(
        character_id="char-1",
        action_type="skill_check",
        reasoning="searches the room",
        outcome="success",
    )
    assert triggered is True


@pytest.mark.asyncio
async def test_normal_action_no_trigger():
    svc, _, _ = _make_service(hidden_state={"sanity": 90, "flags": []})
    triggered, _ = await svc.should_trigger_whisper(
        character_id="char-1",
        action_type="melee_attack",
        reasoning="strikes the goblin with his sword",
        outcome="success",
    )
    assert triggered is False


# ── Rate limiting ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_allows_under_threshold():
    svc, _, redis = _make_service(redis_count=2)
    triggered, _ = await svc.should_trigger_whisper(
        character_id="char-1",
        action_type="sanity_check",
        reasoning="",
        outcome="failure",
    )
    assert triggered is True
    redis.incr.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_blocks_over_threshold():
    svc, _, redis = _make_service(redis_count=4)
    triggered, _ = await svc.should_trigger_whisper(
        character_id="char-1",
        action_type="sanity_check",
        reasoning="",
        outcome="failure",
    )
    assert triggered is False


@pytest.mark.asyncio
async def test_rate_limit_fail_open_on_redis_error():
    """A Redis failure must not suppress the whisper."""
    svc, _, redis = _make_service()
    redis.incr = AsyncMock(side_effect=ConnectionError("redis down"))
    triggered, _ = await svc.should_trigger_whisper(
        character_id="char-1",
        action_type="sanity_check",
        reasoning="",
        outcome="failure",
    )
    assert triggered is True


# ── build_hidden_context ──────────────────────────────────────────────────────


def test_build_hidden_context_empty():
    svc, _, _ = _make_service()
    assert svc.build_hidden_context({}) == ""


def test_build_hidden_context_breakdown():
    svc, _, _ = _make_service()
    result = svc.build_hidden_context({"sanity": 3, "flags": ["cursed"]})
    assert "BREAKDOWN" in result
    assert "cursed" in result


def test_build_hidden_context_paranoid_range():
    svc, _, _ = _make_service()
    result = svc.build_hidden_context({"sanity": 18, "flags": []})
    assert "PARANOID" in result


def test_build_hidden_context_healthy_no_flags():
    """Healthy character with no flags returns empty context."""
    svc, _, _ = _make_service()
    result = svc.build_hidden_context({"sanity": 95, "flags": []})
    assert result == ""


# ── apply_delta ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_delta_calls_db_methods():
    svc, db, _ = _make_service(hidden_state={"sanity": 50, "flags": []})
    await svc.apply_delta(
        character_id="char-1",
        intent_id="intent-1",
        sanity_drain=10,
        flags_add=["paranoid"],
        trigger="horror_witness",
        whisper_text="You feel eyes watching you.",
    )
    db.apply_hidden_state_delta.assert_awaited_once_with(
        character_id="char-1",
        sanity_drain=10,
        flags_add=["paranoid"],
        flags_remove=None,
    )
    db.log_whisper.assert_awaited_once()
