# NodeRouter — Unit Test Coverage

## Summary

Adds `orchestrator/tests/test_node_router.py` — 52 unit tests covering the full public API of `NodeRouter`, the central Ollama-call routing service.

## Context

`NodeRouter` is described in CLAUDE.md as critical infrastructure:
> "Use `NodeRouter` for all Ollama calls. Never hardcode a node URL — nodes are discovered from the `node_registry` table and auto-promoted by TTFT benchmarking."

Despite being one of the most important services in the orchestrator, it had zero test coverage on `main`.

## Approach

All external dependencies (httpx, database, OllamaClient, OpenAICompatClient) are mocked with `unittest.mock`. No real network calls or DB connections are made.

## Coverage by class

| Test class | Method(s) covered | Tests |
|---|---|---|
| `TestProbeNode` | `_probe_node` | 5 |
| `TestMeasureTTFT` | `_measure_ttft` | 7 |
| `TestIsStorytellerEnabled` | `is_storyteller_enabled` | 4 |
| `TestGetStorytellerClient` | `get_storyteller_client` | 5 |
| `TestGetOllamaClientForRole` | `get_ollama_client_for_role` | 6 |
| `TestGetCloudAdjudicator` | `_get_cloud_adjudicator` | 7 |
| `TestGetOllamaClient` | `get_ollama_client` | 4 |
| `TestProbeAndUpdate` | `_probe_and_update` | 5 |
| `TestCheckAllNodes` | `_check_all_nodes` | 3 |
| `TestWarmupAllNodes` | `warmup_all_nodes` | 1 |
| `TestLifecycle` | `start`, `stop` | 4 |
| **Total** | | **51** |

## Key behaviors verified

- `_probe_node`: `online` → HTTP 200, `degraded` → non-200, `offline` → exception
- `_measure_ttft`: returns ms on first non-empty content chunk; skips blank/empty-content frames; returns `None` on any error
- Latency-sorted Auto-Promotion (narrative role) vs. priority-sorted adjudication
- Cloud adjudication: SillyTavern URL cache invalidation on URL change
- `_probe_and_update`: writes both `status` and `latency_ms`; skips `update_node_latency` when TTFT is `None`
- `_check_all_nodes`: filters to `node_type=ollama AND enabled=True`; exceptions from individual probes do not abort the gather
- `start()` fires an immediate probe task in addition to starting the 30s health loop

## Running tests

```bash
pip install pytest pytest-asyncio
pytest orchestrator/tests/test_node_router.py -v
```
