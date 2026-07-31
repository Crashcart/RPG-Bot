## Summary

<!-- 1-3 bullet points describing what this PR does and why -->

-
-

## Type of Change

<!-- Check all that apply -->

- [ ] New feature
- [ ] Bug fix
- [ ] Refactor / cleanup
- [ ] Schema / migration change
- [ ] Infrastructure / Docker change
- [ ] Governance / docs update

---

## Pre-Merge Checklist

### Targeting & Branching
- [ ] This PR targets `alpha` (not `beta` or `main` directly)
- [ ] Source branch is prefixed `claude/` or is a hotfix branch

### Governance Files
- [ ] `.github/TODO.md` updated — completed items marked `[x]`, new items added
- [ ] `.github/PLANNING.md` updated — architectural decisions recorded, handoff notes current

### Schema Contract
- [ ] If `orchestrator/schemas/payloads.py` changed — all pipeline phases still compile
- [ ] No raw dicts passed between pipeline phases (Pydantic models only)

### Database
- [ ] If DB schema changed — a new `db/migrations/0NN_*.sql` file is included
- [ ] New migration file follows `0NN_snake_case.sql` naming convention
- [ ] No existing migration files modified
- [ ] `.env.example` updated if new env vars added to `orchestrator/config.py`

### Naming & Branding
- [ ] New proper nouns, item names, and faction names are sourced from ingested rulebook PDFs
      or existing `story_facts` / `story_entities` — not invented from real-world sources
- [ ] If new named entities were introduced in prompts — brand filtering behaviour reviewed

### AI / LLM Rules
- [ ] Ollama is not used for narration (Phase 4 only: Gemini / Claude / SillyTavern)
- [ ] No hardcoded Ollama node URLs — all calls go through `NodeRouter`
- [ ] Dice outcomes are backend-generated — no LLM path to influence rolls

### Docker / Services
- [ ] No new service added to `docker-compose.yml` without a health check
- [ ] External services (SillyTavern, custom nodes) configured via system_settings, not compose
- [ ] New services join `aetheris_net` (and `aetheris_store` if they need persistence)

### CI
- [ ] All CI checks pass (governance, lint, migrations, schema)

---

## Testing Done

<!-- Describe what you tested and how -->

-

## Migration Instructions (if applicable)

<!-- Paste the exact psql command to apply the migration -->

```bash
docker compose exec ironclad-db psql -U ironclad -d ironclad \
  -f /path/to/0NN_migration.sql
```
