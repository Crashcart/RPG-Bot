"""
System Integrity Check (SIC) — Aetheris Scribe Engine
======================================================
Four-pillar automated verifier that runs on startup, on-demand, and
post-backup to confirm the Aetheris environment is healthy before the
Discord bot is allowed to connect.

Pillars
-------
1. Path Validation   — data/vault/scribe_core.db + genre subdirs  (CRITICAL)
2. Database Health   — PRAGMA integrity_check on SQLite             (CRITICAL)
3. GPU Passthrough   — Ollama /api/ps VRAM probe on Brain           (WARNING)
4. Permission Parity — write/delete test in handouts/ + backups/    (CRITICAL)

Status aggregation
------------------
  "healthy"  — all pillars passed
  "unstable" — one or more WARNING pillars failed (non-critical)
  "critical" — one or more CRITICAL pillars failed (aborts bot connection)

Redis keys written after every run
-----------------------------------
  ironclad:sic:result   — full JSON (no TTL)
  ironclad:sic:status   — "healthy" | "unstable" | "critical" (no TTL)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from orchestrator.services.cache_service import CacheService

logger = logging.getLogger(__name__)

_SIC_RESULT_KEY = "ironclad:sic:result"
_SIC_STATUS_KEY = "ironclad:sic:status"


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class PillarResult:
    name:     str
    passed:   bool
    critical: bool   # True → failure elevates overall status to "critical"
    message:  str
    detail:   str = ""


@dataclass
class SICResult:
    status:     str                        # "healthy" | "unstable" | "critical"
    pillars:    list[PillarResult] = field(default_factory=list)
    checked_at: datetime           = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status":     self.status,
            "checked_at": self.checked_at.isoformat(),
            "pillars": [
                {
                    "name":     p.name,
                    "passed":   p.passed,
                    "critical": p.critical,
                    "message":  p.message,
                    "detail":   p.detail,
                }
                for p in self.pillars
            ],
        }


# ── Service ───────────────────────────────────────────────────────────────────

class SystemIntegrityCheck:
    """
    Orchestrates the four SIC pillars and publishes results to Redis so the
    Pulse dashboard and Discord bot can surface status without re-running checks.
    """

    def __init__(
        self,
        data_dir:    str,
        backups_dir: str,
        ollama_host: str = "http://brain:11434",
        cache: "CacheService | None" = None,
    ) -> None:
        self._data_dir    = Path(data_dir)
        self._backups_dir = Path(backups_dir)
        self._ollama_host = ollama_host.rstrip("/")
        self._cache       = cache

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> SICResult:
        """
        Run all four pillars concurrently.

        Never raises — exceptions within individual pillars are caught and
        converted into failed PillarResults so the overall check always
        returns a usable SICResult.
        """
        raw = await asyncio.gather(
            self._check_paths(),
            self._check_database(),
            self._check_gpu(),
            self._check_permissions(),
            return_exceptions=True,
        )

        _PILLAR_NAMES = [
            "path_validation", "db_health",
            "gpu_passthrough", "permission_parity",
        ]
        pillars: list[PillarResult] = []
        for name, res in zip(_PILLAR_NAMES, raw):
            if isinstance(res, BaseException):
                pillars.append(PillarResult(
                    name=name, passed=False, critical=True,
                    message="Pillar raised an unhandled exception.",
                    detail=str(res),
                ))
            else:
                pillars.append(res)

        any_critical_fail = any(not p.passed and p.critical     for p in pillars)
        any_warn_fail     = any(not p.passed and not p.critical  for p in pillars)

        if any_critical_fail:
            status = "critical"
        elif any_warn_fail:
            status = "unstable"
        else:
            status = "healthy"

        result = SICResult(status=status, pillars=pillars)

        if self._cache:
            asyncio.create_task(self._persist(result))

        logger.info(
            "SIC %-9s | %s",
            status.upper(),
            " | ".join(
                f"{p.name}={'OK' if p.passed else 'FAIL'}" for p in pillars
            ),
        )
        return result

    # ── Pillar 1: Path Validation ─────────────────────────────────────────────

    async def _check_paths(self) -> PillarResult:
        """Confirm scribe_core.db (Reality Anchor) + minimum genre subdirs exist."""
        vault_db = self._data_dir / "vault" / "scribe_core.db"
        missing_critical: list[str] = []
        missing_noncritical: list[str] = []

        if not vault_db.exists():
            missing_critical.append(str(vault_db))

        for subdir in ("fonts", "templates", "handouts"):
            if not (self._data_dir / subdir).is_dir():
                missing_noncritical.append(str(self._data_dir / subdir))

        if missing_critical:
            return PillarResult(
                name="path_validation",
                passed=False,
                critical=True,
                message="Missing Reality Anchor — scribe_core.db not found.",
                detail=", ".join(missing_critical + missing_noncritical),
            )

        if missing_noncritical:
            return PillarResult(
                name="path_validation",
                passed=False,
                critical=False,
                message="Missing non-critical asset directories.",
                detail=", ".join(missing_noncritical),
            )

        return PillarResult(
            name="path_validation",
            passed=True,
            critical=True,
            message="Reality Anchor confirmed. All required paths present.",
        )

    # ── Pillar 2: Database Health ─────────────────────────────────────────────

    async def _check_database(self) -> PillarResult:
        """Execute PRAGMA integrity_check on the SQLite WAL database."""
        db_path = self._data_dir / "vault" / "scribe_core.db"
        if not db_path.exists():
            return PillarResult(
                name="db_health",
                passed=False,
                critical=True,
                message="Cannot run integrity check — scribe_core.db missing.",
                detail=str(db_path),
            )

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, _sqlite_integrity_check, db_path)
        except Exception as exc:
            return PillarResult(
                name="db_health",
                passed=False,
                critical=True,
                message="integrity_check raised an exception.",
                detail=str(exc),
            )

        if result == "ok":
            return PillarResult(
                name="db_health",
                passed=True,
                critical=True,
                message="PRAGMA integrity_check: ok",
            )

        return PillarResult(
            name="db_health",
            passed=False,
            critical=True,
            message="Database corruption detected — bot connection locked.",
            detail=result[:500],
        )

    # ── Pillar 3: GPU Passthrough ─────────────────────────────────────────────

    async def _check_gpu(self) -> PillarResult:
        """
        Probe Brain (Ollama) via /api/ps to detect active VRAM usage.

        A model with size_vram > 0 confirms GPU passthrough (renderD128/card0).
        Falls back to /api/tags liveness check. Returns Neural Latency Warning
        on CPU-only mode or unreachable brain — never blocks startup (non-critical).
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                ps_resp = await client.get(f"{self._ollama_host}/api/ps")
                if ps_resp.status_code == 200:
                    models     = ps_resp.json().get("models", [])
                    vram_total = sum(m.get("size_vram", 0) for m in models)
                    if vram_total > 0:
                        return PillarResult(
                            name="gpu_passthrough",
                            passed=True,
                            critical=False,
                            message=(
                                f"GPU active — {len(models)} model(s) loaded, "
                                f"{vram_total // (1024 * 1024)} MB VRAM in use."
                            ),
                        )

                # Brain alive but no VRAM — confirm liveness first
                tags_resp = await client.get(f"{self._ollama_host}/api/tags")
                if tags_resp.status_code == 200:
                    model_count = len(tags_resp.json().get("models", []))
                    return PillarResult(
                        name="gpu_passthrough",
                        passed=False,
                        critical=False,
                        message=(
                            "Neural Latency Warning — Brain is online but no VRAM "
                            "detected. Falling back to CPU mode."
                        ),
                        detail=f"{model_count} model(s) available on brain.",
                    )

        except httpx.ConnectError:
            pass
        except Exception as exc:
            logger.debug("SIC GPU probe error (non-fatal): %s", exc)

        return PillarResult(
            name="gpu_passthrough",
            passed=False,
            critical=False,
            message="Neural Latency Warning — Brain (Ollama) is unreachable.",
            detail=f"Tried: {self._ollama_host}",
        )

    # ── Pillar 4: Permission Parity ───────────────────────────────────────────

    async def _check_permissions(self) -> PillarResult:
        """
        Write and immediately delete a sentinel file in handouts/ and backups/.
        Failure indicates a UID/GID mismatch between the container user and
        the mounted volume — a "Vault Lockout" condition.
        """
        loop     = asyncio.get_running_loop()
        failures: list[str] = []

        for target_dir in (self._data_dir / "handouts", self._backups_dir):
            try:
                await loop.run_in_executor(None, _permission_probe, target_dir)
            except Exception as exc:
                failures.append(f"{target_dir.name}: {exc}")

        if failures:
            return PillarResult(
                name="permission_parity",
                passed=False,
                critical=True,
                message="Vault Lockout — write/delete probe failed (UID/GID mismatch).",
                detail="; ".join(failures),
            )

        return PillarResult(
            name="permission_parity",
            passed=True,
            critical=True,
            message="Permission parity confirmed for handouts/ and backups/.",
        )

    # ── Redis Persistence ─────────────────────────────────────────────────────

    async def _persist(self, result: SICResult) -> None:
        """Write result JSON and status string to Redis (no TTL)."""
        try:
            payload = json.dumps(result.to_dict())
            await self._cache.set(_SIC_RESULT_KEY, payload)
            await self._cache.set(_SIC_STATUS_KEY, result.status)
        except Exception as exc:
            logger.warning("SIC: failed to persist result to Redis: %s", exc)


# ── Thread-safe helpers (run in executor) ─────────────────────────────────────

def _sqlite_integrity_check(db_path: Path) -> str:
    """Open the DB read-only and run PRAGMA integrity_check."""
    import sqlite3
    with sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, timeout=5, check_same_thread=False
    ) as conn:
        rows = conn.execute("PRAGMA integrity_check;").fetchall()
    return rows[0][0] if rows else "no result"


def _permission_probe(target_dir: Path) -> None:
    """Create and immediately delete a .sic_probe sentinel file."""
    target_dir.mkdir(parents=True, exist_ok=True)
    probe = target_dir / ".sic_probe"
    probe.write_text("sic", encoding="utf-8")
    probe.unlink()
