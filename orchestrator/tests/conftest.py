"""
pytest conftest — loads PropheticBuffer and JanitorService directly from
their source files, completely bypassing orchestrator/services/__init__.py
and its heavy transitive dependencies.

The loaded modules are exposed as session-scoped fixtures AND patched into
sys.modules under their real dotted names so that `import` statements inside
the modules under test resolve correctly.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Project root (two levels up from this file: tests/ → orchestrator/ → project)
_PROJ = Path(__file__).parent.parent.parent
_SVC  = _PROJ / "orchestrator" / "services"
_SCH  = _PROJ / "orchestrator" / "schemas"


def _load(dotted: str, file_path: Path) -> types.ModuleType:
    """Load a single .py file as a module with a given dotted name."""
    spec = importlib.util.spec_from_file_location(dotted, file_path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Load schemas/payloads.py (no heavy deps) ─────────────────────────────────
# Pydantic IS available; only asyncpg/redis/chromadb chains are absent.
_payloads = _load("orchestrator.schemas.payloads", _SCH / "payloads.py")

# Expose as orchestrator.schemas so "from orchestrator.schemas.payloads import X" works
_sch_pkg = types.ModuleType("orchestrator.schemas")
_sch_pkg.payloads = _payloads
sys.modules.setdefault("orchestrator", types.ModuleType("orchestrator"))
sys.modules["orchestrator.schemas"] = _sch_pkg

# ── Stub the cache and storyteller interfaces needed by prophetic_buffer ──────
# These are TYPE_CHECKING-only imports in the module, so no stubs needed
# at runtime — the module uses string annotations for them.

# ── Load prophetic_buffer.py directly ────────────────────────────────────────
_pb = _load("orchestrator.services.prophetic_buffer", _SVC / "prophetic_buffer.py")

# ── Load janitor.py directly ─────────────────────────────────────────────────
_jan = _load("orchestrator.services.janitor", _SVC / "janitor.py")

# ── Register orchestrator.services package so patch() can resolve the dotted
#    names "orchestrator.services.prophetic_buffer" and "orchestrator.services.janitor"
_svc_pkg = types.ModuleType("orchestrator.services")
_svc_pkg.prophetic_buffer = _pb
_svc_pkg.janitor = _jan
sys.modules["orchestrator.services"] = _svc_pkg
# Make orchestrator package aware of .services
sys.modules["orchestrator"].services = _svc_pkg  # type: ignore[attr-defined]
