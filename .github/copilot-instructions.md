# Ironclad GM — Agent Instructions

Rules for all AI agents (Claude Code, Copilot, and any future agents) working in this repository.
These rules are non-negotiable and override any default agent behaviour.

---

## Branch Rules

- **Always** develop on a `claude/<description>-<session-id>` branch.
- **Never** push directly to `alpha`, `beta`, or `main`.
- PRs from `claude/*` branches must target `alpha`, never `main`.
- Branch names must be lowercase, hyphen-separated, and end with the session ID.

---

## Pipeline Contract (read CLAUDE.md §Key Architectural Rules)

- All inter-service data passes through Pydantic models in `orchestrator/schemas/payloads.py`.
  Never pass raw dicts between pipeline phases.
- **Ollama never narrates.** Phase 2 produces only `OllamaResolutionPayload`. Narrative prose is
  Phase 4 only (GMDirector → Gemini / Claude / SillyTavern).
- **Dice are backend-only.** The LLM requests a roll via `DiceRequest`; the backend generates the
  result. The model cannot influence dice outcomes.
- **NodeRouter for all Ollama calls.** Never hardcode an Ollama node URL. All calls go through
  `NodeRouter.get_ollama_client_for_role()`.
- `action_log` rows are immutable. Retcons set `retconned=TRUE`; rows are never deleted.

---

## Naming and Branding Rules

- **In-world names and terminology must come from ingested PDF rulebooks** (retrieved via RAG from
  ChromaDB). Names that appear in the active campaign's rulebooks are allowed in generated narrative.
- **Real-world brand names are blocked** unless the name appears in both the real world AND in an
  ingested rulebook (e.g. a licensed RPG product that uses real brand names). When in doubt, use a
  generic equivalent.
- The sub-agent post-processor applies brand filtering to all `SubAgentResult` output.
  If filtering fails after retry, `brand_violation=True` is set — do not suppress this flag.
- Do not introduce new proper nouns, weapon names, faction names, or lore terms that are not
  sourced from the campaign's rulebooks or the `story_facts` / `story_entities` tables.

---

## Database Rules

- New migrations go in `db/migrations/0NN_<snake_case>.sql`. The `NN` must be the next sequential
  number. **Never modify an existing migration file.**
- Schema changes require a migration. Never ALTER TABLE in application code.
- `global_settings` / `system_settings` seeds belong in the migration that introduces the feature.
- Always use asyncpg via `DatabaseService` — never a raw connection string in application code.

---

## Docker / Service Rules

- Service names follow the `aetheris_*` network convention defined in `docker-compose.yml`.
- New services must join `aetheris_net`. Services that need persistent storage also join
  `aetheris_store`.
- Health checks are required for every new service.
- Never hardcode ports in application code — always read from environment variables or service
  discovery.
- External services (SillyTavern, custom Ollama nodes, etc.) are configured via system_settings
  and are **not** added to `docker-compose.yml`.

---

## Code Quality Rules

- All new Python files must pass `ruff check` with zero errors.
- No `print()` statements — use `logging.getLogger(__name__)`.
- No bare `except:` clauses — always catch a specific exception type.
- Type hints required on all public function signatures.
- Async functions must use `await` for all I/O — no blocking calls in coroutines.
- Do not add backwards-compatibility shims for removed code. Delete cleanly.

---

## Testing and Verification

- After any change to `orchestrator/schemas/payloads.py`, run:
  `python -m py_compile orchestrator/schemas/payloads.py`
- After any migration, verify it applies cleanly:
  `docker compose run --rm ironclad-db psql -U ironclad -c "SELECT 1"`
- The CI workflow (`.github/workflows/ci.yml`) must pass before any PR is approved.

---

## Governance File Maintenance

When completing a significant task, update:
- `.github/TODO.md` — mark completed items, add any new items discovered
- `.github/PLANNING.md` — record architectural decisions made and handoff notes
- These files must be updated in the same commit as the feature work, not as a follow-up.
