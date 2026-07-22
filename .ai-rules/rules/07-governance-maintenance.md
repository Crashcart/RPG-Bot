## Governance File Maintenance

When completing a significant task, update in the same commit:
- `.github/TODO.md` — mark completed items `[x]`, add newly discovered items
- `.github/PLANNING.md` — record architectural decisions made and handoff notes

After any change to the rule files under `.ai-rules/rules/`, run:
  `bash .ai-rules/scripts/sync-rules.sh`

This regenerates `.github/copilot-instructions.md` and `CLAUDE.md` from the modular sources.
Commit the regenerated files alongside the rule change — never edit the targets directly.
