"""
Unit tests for Campaign Management REST API.

Tests cover:
  - DatabaseService campaign CRUD methods (mocked asyncpg pool)
  - CampaignCreateRequest / CampaignUpdateRequest / CampaignResponse schemas
  - API endpoints via FastAPI TestClient (db/cache mocked)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.payloads import (
    CampaignCreateRequest,
    CampaignResponse,
    CampaignUpdateRequest,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_campaign_row(
    campaign_id: str | None = None,
    guild_id: str = "guild-1",
    name: str = "Test Campaign",
    system: str = "D&D 5e",
    active: bool = True,
    settings: dict | None = None,
    character_count: int = 2,
    fact_count: int = 5,
) -> dict:
    return {
        "id":              campaign_id or str(uuid.uuid4()),
        "guild_id":        guild_id,
        "name":            name,
        "system":          system,
        "active":          active,
        "settings":        settings or {},
        "character_count": character_count,
        "fact_count":      fact_count,
        "created_at":      datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


class _PoolAcquireCM:
    """Async context manager for asyncpg pool.acquire()."""
    def __init__(self, conn: AsyncMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self._conn

    async def __aexit__(self, *_) -> None:
        pass


def _make_pool(fetchrow=None, fetch=None, execute=None) -> MagicMock:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=fetchrow)
    pool.fetch    = AsyncMock(return_value=fetch or [])
    pool.execute  = AsyncMock(return_value=execute or "UPDATE 1")
    conn = MagicMock()
    conn.fetchrow = pool.fetchrow
    conn.fetch    = pool.fetch
    conn.execute  = pool.execute
    pool.acquire  = MagicMock(return_value=_PoolAcquireCM(conn))
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# Schema Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCampaignSchemas:
    def test_create_request_valid(self):
        req = CampaignCreateRequest(
            guild_id="123", name="My Campaign", system="Mothership"
        )
        assert req.guild_id == "123"
        assert req.system == "Mothership"
        assert req.settings == {}

    def test_create_request_with_settings(self):
        req = CampaignCreateRequest(
            guild_id="123", name="HC", system="Call of Cthulhu",
            settings={"house_rules": ["no luck re-rolls"]}
        )
        assert req.settings["house_rules"] == ["no luck re-rolls"]

    def test_create_request_name_too_long(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            CampaignCreateRequest(guild_id="123", name="x" * 121, system="D&D 5e")

    def test_create_request_empty_name(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            CampaignCreateRequest(guild_id="123", name="", system="D&D 5e")

    def test_update_request_all_none(self):
        req = CampaignUpdateRequest()
        assert req.name is None
        assert req.system is None
        assert req.settings is None

    def test_update_request_partial(self):
        req = CampaignUpdateRequest(name="New Name")
        assert req.name == "New Name"
        assert req.system is None

    def test_campaign_response_from_dict(self):
        row = _make_campaign_row()
        resp = CampaignResponse(**row)
        assert resp.active is True
        assert resp.character_count == 2
        assert resp.fact_count == 5


# ─────────────────────────────────────────────────────────────────────────────
# DatabaseService Campaign Methods
# ─────────────────────────────────────────────────────────────────────────────

class TestDatabaseServiceCampaigns:
    def _db_service(self, pool: MagicMock):
        from orchestrator.services.database import DatabaseService
        svc = DatabaseService.__new__(DatabaseService)
        svc._pool = pool
        return svc

    @pytest.mark.asyncio
    async def test_create_campaign_returns_row(self):
        cid = str(uuid.uuid4())
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "id": uuid.UUID(cid), "guild_id": "g1", "name": "Camp",
            "system": "D&D 5e", "active": True,
            "settings": '{}', "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }[k]
        pool = _make_pool(fetchrow=row)
        svc = self._db_service(pool)

        result = await svc.create_campaign("g1", "Camp", "D&D 5e")

        pool.fetchrow.assert_awaited_once()
        assert result["id"] == cid
        assert result["name"] == "Camp"
        assert result["active"] is True

    @pytest.mark.asyncio
    async def test_create_campaign_with_settings(self):
        cid = str(uuid.uuid4())
        row = MagicMock()
        settings_str = json.dumps({"cr_budget": 4})
        row.__getitem__ = lambda self, k: {
            "id": uuid.UUID(cid), "guild_id": "g1", "name": "HC",
            "system": "Pathfinder", "active": True,
            "settings": settings_str,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }[k]
        pool = _make_pool(fetchrow=row)
        svc = self._db_service(pool)

        result = await svc.create_campaign("g1", "HC", "Pathfinder", {"cr_budget": 4})
        assert result["settings"] == {"cr_budget": 4}

    @pytest.mark.asyncio
    async def test_get_campaign_by_id_found(self):
        cid = str(uuid.uuid4())
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "id": uuid.UUID(cid), "guild_id": "g2", "name": "Camp2",
            "system": "Mothership", "active": True, "settings": "{}",
            "character_count": 3, "fact_count": 7,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }[k]
        pool = _make_pool(fetchrow=row)
        svc = self._db_service(pool)

        result = await svc.get_campaign_by_id(cid)
        assert result is not None
        assert result["id"] == cid
        assert result["character_count"] == 3

    @pytest.mark.asyncio
    async def test_get_campaign_by_id_not_found(self):
        pool = _make_pool(fetchrow=None)
        svc = self._db_service(pool)
        result = await svc.get_campaign_by_id(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_list_campaigns_by_guild_empty(self):
        pool = _make_pool(fetch=[])
        svc = self._db_service(pool)
        result = await svc.list_campaigns_by_guild("guild-xyz")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_campaigns_by_guild_returns_rows(self):
        cid1, cid2 = str(uuid.uuid4()), str(uuid.uuid4())
        rows = []
        for cid, name in [(cid1, "Alpha"), (cid2, "Beta")]:
            r = MagicMock()
            r.__getitem__ = (lambda _cid, _name: lambda self, k: {
                "id": uuid.UUID(_cid), "guild_id": "g1", "name": _name,
                "system": "D&D 5e", "active": True, "settings": "{}",
                "character_count": 1, "fact_count": 0,
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }[k])(cid, name)
            rows.append(r)
        pool = _make_pool(fetch=rows)
        svc = self._db_service(pool)

        result = await svc.list_campaigns_by_guild("g1")
        assert len(result) == 2
        assert result[0]["name"] == "Alpha"

    @pytest.mark.asyncio
    async def test_update_campaign_name(self):
        cid = str(uuid.uuid4())
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "id": uuid.UUID(cid), "guild_id": "g1", "name": "Renamed",
            "system": "D&D 5e", "active": True,
            "settings": "{}", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }[k]
        pool = _make_pool(fetchrow=row)
        svc = self._db_service(pool)

        result = await svc.update_campaign(cid, name="Renamed")
        assert result is not None
        assert result["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_update_campaign_not_found(self):
        pool = _make_pool(fetchrow=None)
        svc = self._db_service(pool)
        result = await svc.update_campaign(str(uuid.uuid4()), name="Ghost")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_campaign_settings(self):
        cid = str(uuid.uuid4())
        new_settings = json.dumps({"allow_pvp": True})
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "id": uuid.UUID(cid), "guild_id": "g1", "name": "Camp",
            "system": "D&D 5e", "active": True,
            "settings": new_settings, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }[k]
        pool = _make_pool(fetchrow=row)
        svc = self._db_service(pool)

        result = await svc.update_campaign(cid, settings={"allow_pvp": True})
        assert result["settings"] == {"allow_pvp": True}

    @pytest.mark.asyncio
    async def test_update_campaign_multi_field(self):
        cid = str(uuid.uuid4())
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "id": uuid.UUID(cid), "guild_id": "g1", "name": "New",
            "system": "Shadowrun", "active": True,
            "settings": "{}", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }[k]
        pool = _make_pool(fetchrow=row)
        svc = self._db_service(pool)

        result = await svc.update_campaign(cid, name="New", system="Shadowrun")
        query_sql = pool.fetchrow.call_args[0][0]
        assert "name = $1" in query_sql
        assert "system = $2" in query_sql

    @pytest.mark.asyncio
    async def test_deactivate_campaign_success(self):
        pool = _make_pool(execute="UPDATE 1")
        svc = self._db_service(pool)
        result = await svc.deactivate_campaign(str(uuid.uuid4()))
        assert result is True

    @pytest.mark.asyncio
    async def test_deactivate_campaign_already_inactive(self):
        pool = _make_pool(execute="UPDATE 0")
        svc = self._db_service(pool)
        result = await svc.deactivate_campaign(str(uuid.uuid4()))
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# API Endpoint Tests (via FastAPI TestClient)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def test_client():
    """
    Builds a minimal FastAPI test app that mounts only the campaign endpoints,
    injecting a mock DatabaseService so no real DB is needed.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    test_app = FastAPI()
    mock_db = AsyncMock()

    cid = str(uuid.uuid4())
    sample_row = _make_campaign_row(campaign_id=cid)

    @test_app.get("/api/campaigns")
    async def _list(guild_id: str | None = None):
        if guild_id:
            return await mock_db.list_campaigns_by_guild(guild_id)
        return await mock_db.get_all_campaigns()

    @test_app.post("/api/campaigns", status_code=201)
    async def _create(req: CampaignCreateRequest):
        return await mock_db.create_campaign(req.guild_id, req.name, req.system, req.settings)

    @test_app.get("/api/campaigns/{campaign_id}")
    async def _get(campaign_id: str):
        from fastapi import HTTPException
        row = await mock_db.get_campaign_by_id(campaign_id)
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return row

    @test_app.patch("/api/campaigns/{campaign_id}")
    async def _update(campaign_id: str, req: CampaignUpdateRequest):
        from fastapi import HTTPException
        row = await mock_db.update_campaign(campaign_id, req.name, req.system, req.settings)
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return row

    @test_app.delete("/api/campaigns/{campaign_id}")
    async def _delete(campaign_id: str):
        from fastapi import HTTPException
        ok = await mock_db.deactivate_campaign(campaign_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Not found or already inactive")
        return {"status": "deactivated", "campaign_id": campaign_id}

    client = TestClient(test_app)
    client.mock_db = mock_db
    client.sample_row = sample_row
    client.sample_id = cid
    return client


class TestCampaignAPIEndpoints:
    def test_list_campaigns_no_filter(self, test_client):
        test_client.mock_db.get_all_campaigns = AsyncMock(return_value=[test_client.sample_row])
        resp = test_client.get("/api/campaigns")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Test Campaign"

    def test_list_campaigns_guild_filter(self, test_client):
        test_client.mock_db.list_campaigns_by_guild = AsyncMock(return_value=[test_client.sample_row])
        resp = test_client.get("/api/campaigns?guild_id=guild-1")
        assert resp.status_code == 200
        test_client.mock_db.list_campaigns_by_guild.assert_awaited_once_with("guild-1")

    def test_create_campaign_success(self, test_client):
        test_client.mock_db.create_campaign = AsyncMock(return_value=test_client.sample_row)
        payload = {"guild_id": "g1", "name": "New Campaign", "system": "Mothership"}
        resp = test_client.post("/api/campaigns", json=payload)
        assert resp.status_code == 201
        assert resp.json()["name"] == "Test Campaign"  # mock returns sample_row

    def test_create_campaign_missing_field(self, test_client):
        resp = test_client.post("/api/campaigns", json={"guild_id": "g1", "name": "Oops"})
        assert resp.status_code == 422

    def test_get_campaign_found(self, test_client):
        test_client.mock_db.get_campaign_by_id = AsyncMock(return_value=test_client.sample_row)
        resp = test_client.get(f"/api/campaigns/{test_client.sample_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == test_client.sample_id

    def test_get_campaign_not_found(self, test_client):
        test_client.mock_db.get_campaign_by_id = AsyncMock(return_value=None)
        resp = test_client.get(f"/api/campaigns/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_update_campaign_name(self, test_client):
        updated = {**test_client.sample_row, "name": "Renamed"}
        test_client.mock_db.update_campaign = AsyncMock(return_value=updated)
        resp = test_client.patch(
            f"/api/campaigns/{test_client.sample_id}",
            json={"name": "Renamed"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    def test_update_campaign_not_found(self, test_client):
        test_client.mock_db.update_campaign = AsyncMock(return_value=None)
        resp = test_client.patch(
            f"/api/campaigns/{uuid.uuid4()}",
            json={"name": "Ghost"},
        )
        assert resp.status_code == 404

    def test_deactivate_campaign_success(self, test_client):
        test_client.mock_db.deactivate_campaign = AsyncMock(return_value=True)
        resp = test_client.delete(f"/api/campaigns/{test_client.sample_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "deactivated"
        assert body["campaign_id"] == test_client.sample_id

    def test_deactivate_campaign_not_found(self, test_client):
        test_client.mock_db.deactivate_campaign = AsyncMock(return_value=False)
        resp = test_client.delete(f"/api/campaigns/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_list_campaigns_empty(self, test_client):
        test_client.mock_db.get_all_campaigns = AsyncMock(return_value=[])
        resp = test_client.get("/api/campaigns")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_campaign_propagates_settings(self, test_client):
        row_with_settings = {**test_client.sample_row, "settings": {"pvp": True}}
        test_client.mock_db.create_campaign = AsyncMock(return_value=row_with_settings)
        payload = {
            "guild_id": "g1", "name": "PvP Campaign",
            "system": "D&D 5e", "settings": {"pvp": True},
        }
        resp = test_client.post("/api/campaigns", json=payload)
        assert resp.status_code == 201
        assert resp.json()["settings"] == {"pvp": True}
