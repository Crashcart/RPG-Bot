"""
Unit tests for CampaignVault (Issue #9 — Multi-Tenant Campaign Vault).

Uses real on-disk SQLite in a pytest tmp_path so no SQLite mocking is needed.
All tests run fast because WAL-mode SQLite on a tmpfs temp dir is very quick.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from orchestrator.services.campaign_vault import CampaignVault, VaultError


@pytest.fixture
def tmp_vault(tmp_path):
    return CampaignVault(data_dir=str(tmp_path))


@pytest_asyncio.fixture
async def vault(tmp_vault):
    await tmp_vault.init()
    return tmp_vault


# ── validate_campaign_id ────────────────────────────────────────────────────────────────────────

class TestValidateCampaignId:
    def test_valid_uuid_lowercase(self):
        cid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert CampaignVault.validate_campaign_id(cid) == cid

    def test_valid_uuid_uppercase_normalised(self):
        cid = "A1B2C3D4-E5F6-7890-ABCD-EF1234567890"
        assert CampaignVault.validate_campaign_id(cid) == cid.lower()

    def test_invalid_not_uuid(self):
        with pytest.raises(VaultError, match="UUID"):
            CampaignVault.validate_campaign_id("not-a-uuid")

    def test_invalid_traversal_attempt(self):
        with pytest.raises(VaultError):
            CampaignVault.validate_campaign_id("../../../etc/passwd")

    def test_invalid_empty(self):
        with pytest.raises(VaultError):
            CampaignVault.validate_campaign_id("")

    def test_invalid_non_string(self):
        with pytest.raises(VaultError):
            CampaignVault.validate_campaign_id(12345)  # type: ignore[arg-type]


# ── provision / destroy ─────────────────────────────────────────────────────────────────────

class TestProvision:
    @pytest.mark.asyncio
    async def test_provision_creates_db_file(self, vault):
        cid = str(uuid.uuid4())
        db_path = await vault.provision(cid)
        assert db_path.exists()

    @pytest.mark.asyncio
    async def test_provision_idempotent(self, vault):
        cid = str(uuid.uuid4())
        path1 = await vault.provision(cid)
        path2 = await vault.provision(cid)
        assert path1 == path2

    @pytest.mark.asyncio
    async def test_provision_invalid_id_raises(self, vault):
        with pytest.raises(VaultError):
            await vault.provision("bad-id")

    @pytest.mark.asyncio
    async def test_destroy_removes_file(self, vault):
        cid = str(uuid.uuid4())
        await vault.provision(cid)
        assert await vault.destroy(cid) is True
        assert not vault._safe_db_path(cid).exists()

    @pytest.mark.asyncio
    async def test_destroy_nonexistent_returns_false(self, vault):
        cid = str(uuid.uuid4())
        assert await vault.destroy(cid) is False


# ── KV store ──────────────────────────────────────────────────────────────────────────────

class TestKVStore:
    @pytest.mark.asyncio
    async def test_kv_set_and_get(self, vault):
        cid = str(uuid.uuid4())
        await vault.provision(cid)
        await vault.kv_set(cid, "player_count", 4)
        assert await vault.kv_get(cid, "player_count") == 4

    @pytest.mark.asyncio
    async def test_kv_get_missing_key_returns_none(self, vault):
        cid = str(uuid.uuid4())
        await vault.provision(cid)
        assert await vault.kv_get(cid, "nonexistent") is None

    @pytest.mark.asyncio
    async def test_kv_get_missing_vault_returns_none(self, vault):
        cid = str(uuid.uuid4())
        assert await vault.kv_get(cid, "any_key") is None

    @pytest.mark.asyncio
    async def test_kv_set_overwrites(self, vault):
        cid = str(uuid.uuid4())
        await vault.provision(cid)
        await vault.kv_set(cid, "state", "draft")
        await vault.kv_set(cid, "state", "active")
        assert await vault.kv_get(cid, "state") == "active"

    @pytest.mark.asyncio
    async def test_kv_stores_complex_value(self, vault):
        cid = str(uuid.uuid4())
        await vault.provision(cid)
        payload = {"name": "Grim", "hp": 32, "inventory": ["sword", "potion"]}
        await vault.kv_set(cid, "character", payload)
        assert await vault.kv_get(cid, "character") == payload

    @pytest.mark.asyncio
    async def test_kv_delete(self, vault):
        cid = str(uuid.uuid4())
        await vault.provision(cid)
        await vault.kv_set(cid, "temp", "value")
        await vault.kv_delete(cid, "temp")
        assert await vault.kv_get(cid, "temp") is None

    @pytest.mark.asyncio
    async def test_kv_delete_missing_key_is_noop(self, vault):
        cid = str(uuid.uuid4())
        await vault.provision(cid)
        await vault.kv_delete(cid, "ghost_key")  # must not raise


# ── Hibernation ────────────────────────────────────────────────────────────────────────────

class TestHibernation:
    @pytest.mark.asyncio
    async def test_hibernate_and_rehydrate(self, vault):
        cid = str(uuid.uuid4())
        snapshot = {"session": "abc123", "turn": 7, "flags": {"combat": True}}
        await vault.hibernate(cid, snapshot)
        assert await vault.rehydrate(cid) == snapshot

    @pytest.mark.asyncio
    async def test_rehydrate_no_vault_returns_empty(self, vault):
        cid = str(uuid.uuid4())
        assert await vault.rehydrate(cid) == {}

    @pytest.mark.asyncio
    async def test_rehydrate_no_snapshot_returns_empty(self, vault):
        cid = str(uuid.uuid4())
        await vault.provision(cid)
        assert await vault.rehydrate(cid) == {}

    @pytest.mark.asyncio
    async def test_hibernate_overwrites_previous(self, vault):
        cid = str(uuid.uuid4())
        await vault.hibernate(cid, {"turn": 1})
        await vault.hibernate(cid, {"turn": 99})
        assert (await vault.rehydrate(cid))["turn"] == 99

    @pytest.mark.asyncio
    async def test_clear_snapshot_after_rehydrate(self, vault):
        cid = str(uuid.uuid4())
        await vault.hibernate(cid, {"data": "exists"})
        await vault.clear_snapshot(cid)
        assert await vault.rehydrate(cid) == {}

    @pytest.mark.asyncio
    async def test_clear_snapshot_noop_on_missing_vault(self, vault):
        cid = str(uuid.uuid4())
        await vault.clear_snapshot(cid)  # must not raise


# ── Isolation ─────────────────────────────────────────────────────────────────────────────────

class TestIsolation:
    @pytest.mark.asyncio
    async def test_campaigns_are_isolated(self, vault):
        cid_a = str(uuid.uuid4())
        cid_b = str(uuid.uuid4())
        await vault.provision(cid_a)
        await vault.provision(cid_b)
        await vault.kv_set(cid_a, "world", "Shadowrun")
        await vault.kv_set(cid_b, "world", "Mothership")
        assert await vault.kv_get(cid_a, "world") == "Shadowrun"
        assert await vault.kv_get(cid_b, "world") == "Mothership"

    @pytest.mark.asyncio
    async def test_list_vaults(self, vault):
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for cid in ids:
            await vault.provision(cid)
        found = await vault.list_vaults()
        for cid in ids:
            assert cid in found

    @pytest.mark.asyncio
    async def test_list_vaults_empty(self, vault):
        assert await vault.list_vaults() == []

    @pytest.mark.asyncio
    async def test_hibernation_snapshots_are_isolated(self, vault):
        cid_a = str(uuid.uuid4())
        cid_b = str(uuid.uuid4())
        await vault.hibernate(cid_a, {"player": "Alice"})
        await vault.hibernate(cid_b, {"player": "Bob"})
        snap_a = await vault.rehydrate(cid_a)
        snap_b = await vault.rehydrate(cid_b)
        assert snap_a["player"] == "Alice"
        assert snap_b["player"] == "Bob"
