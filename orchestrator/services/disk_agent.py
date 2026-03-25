"""
Disk Agency Service — AI World Artifact File System
=====================================================
The GM AI is sandboxed but possesses the authority to write files directly to
the local disk and recursively read those files for self-iteration.

Sandbox Root
------------
All I/O is confined to:  {world_data_dir}/{campaign_id}/

Path traversal is blocked — any path containing '..' or starting with '/' is
rejected.  The AI cannot escape the campaign sandbox.

Files Written by the GM
------------------------
  maps/          Hand-drawn or procedurally generated world maps (SVG/text)
  lore_notes/    World-building text the GM wants to remember across sessions
  code/          Python snippets for custom mechanical rules (read back at runtime)
  session_logs/  Auto-generated session summaries for long-term continuity

API Surface
-----------
  Exposed via /api/disk/* in main.py for authenticated admin and sandbox use.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_BLOCKED_CHARS = {"<", ">", "|", ";", "&", "$", "`"}


class DiskAgentService:
    def __init__(self, world_data_dir: str) -> None:
        self._root = Path(world_data_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    # ── Path Safety ───────────────────────────────────────────────────────────

    def _safe_path(self, campaign_id: str, rel_path: str) -> Path:
        """
        Resolve and validate a campaign-scoped path.

        Raises ValueError if the resolved path escapes the campaign sandbox.
        """
        if not campaign_id or ".." in campaign_id or "/" in campaign_id:
            raise ValueError(f"Invalid campaign_id: {campaign_id!r}")
        if not rel_path:
            raise ValueError("rel_path must not be empty.")

        # Reject obviously dangerous characters
        for ch in _BLOCKED_CHARS:
            if ch in rel_path:
                raise ValueError(f"Disallowed character {ch!r} in path.")

        base = self._root / campaign_id
        target = (base / rel_path).resolve()

        # Ensure the resolved path stays within the campaign directory
        base_resolved = base.resolve()
        try:
            target.relative_to(base_resolved)
        except ValueError:
            raise ValueError(
                f"Path traversal blocked: {rel_path!r} resolves outside sandbox."
            )

        return target

    # ── Write ─────────────────────────────────────────────────────────────────

    async def write(self, campaign_id: str, rel_path: str, content: str) -> dict:
        """
        Write *content* to {world_data_dir}/{campaign_id}/{rel_path}.

        Creates intermediate directories as needed.
        Returns {"path": str, "bytes_written": int}.
        """
        target = self._safe_path(campaign_id, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        target.write_bytes(encoded)
        logger.info("DiskAgent: wrote %d bytes → %s", len(encoded), target)
        return {"path": str(target.relative_to(self._root)), "bytes_written": len(encoded)}

    # ── Read ──────────────────────────────────────────────────────────────────

    async def read(self, campaign_id: str, rel_path: str) -> str:
        """
        Read and return the text content of a sandboxed file.

        Raises FileNotFoundError if the path does not exist.
        """
        target = self._safe_path(campaign_id, rel_path)
        if not target.exists():
            raise FileNotFoundError(f"No world file at: {rel_path}")
        return target.read_text("utf-8")

    # ── List ──────────────────────────────────────────────────────────────────

    async def list_files(self, campaign_id: str, subdir: str = "") -> list[dict]:
        """
        Recursively list all files in the campaign sandbox (or a sub-directory).

        Returns list of {"path": str, "size_bytes": int, "modified": str}.
        """
        if subdir:
            root = self._safe_path(campaign_id, subdir)
        else:
            root = self._root / campaign_id
            root.mkdir(parents=True, exist_ok=True)

        results: list[dict] = []
        if not root.exists():
            return results

        for entry in sorted(root.rglob("*")):
            if entry.is_file():
                stat = entry.stat()
                results.append({
                    "path":       str(entry.relative_to(self._root / campaign_id)),
                    "size_bytes": stat.st_size,
                    "modified":   str(int(stat.st_mtime)),
                })
        return results

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete(self, campaign_id: str, rel_path: str) -> bool:
        """
        Delete a file from the campaign sandbox.

        Returns True if deleted, False if not found.
        """
        target = self._safe_path(campaign_id, rel_path)
        if not target.exists():
            return False
        target.unlink()
        logger.info("DiskAgent: deleted %s", target)
        return True
