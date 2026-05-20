# Claude Code Instructions — RPG-Bot

## What This Repo Is

Discord RPG bot that manages character sheets, game state, and interactive storytelling sessions with persistent per-server data.

## AI-Rules

This repo follows the Crashcart AI-rules system.

- Rules source: `https://github.com/crashcart/ai-rules` (set in `.claude/settings.json` as `rulesRepo`)
- Governing files: `rules/claude.md` + `rules/universal.md`
- Current version in force: check `rulesVersion` in `.claude/settings.json`

The PreToolUse hook in `.claude/settings.json` calls `scripts/check-rules-updates.sh` on every
Bash tool call (rate-limited to once per hour). If it prints "Rules updated to vX.Y.Z", stop
and re-read your rules before continuing.

## Session Start Checklist

1. Check hook output — if it says "Rules updated", re-read `rules/` before anything else
2. Check for a `TODO.md` — if it exists, review open items from the last session
3. Confirm the active branch is `dev` (or a feature branch) — never `main`
4. Check for any resolved tickets that were opened by this repo's AI

## Branching Policy

- All work goes to `dev` or a feature branch off `dev`
- `dev` → `beta` → `main` via PR only — never push directly to `main`
- Branch naming: `type/short-description` (e.g., `feat/add-inventory-cmd`, `fix/xp-overflow`)

[NON-NEGOTIABLE]

## Rule-Edit Suggestions

If you want to suggest a change to any rule in ai-rules, do not modify the file directly.
Open a ticket in the ai-rules repo using `tickets/template.md`:
- Set **Scope** to `rule-edit`
- Set **Requesting AI** to your ai-id
- Claude (CEO) will discuss the change with you before implementing anything

## Python Standards

- **Type hints on all function signatures** — no untyped functions
- `f-strings` over `.format()` or `%`
- `pathlib` over `os.path` for all filesystem operations
- Dataclasses for data containers — not dicts with magic string keys
- Pydantic models are the contract between services — use them, don't bypass them
- Black enforced on save via PostToolUse hook

## Discord Bot Conventions

- Use `discord.py` (or `py-cord`) slash commands — not legacy prefix commands
- All command responses must be ephemeral unless explicitly public
- Rate-limit sensitive commands (character edits, bank transfers)
- Never store Discord tokens or bot secrets in code — use environment variables

## Repo Structure

```
RPG-Bot/
├── src/
│   └── rpgbot/
│       ├── __init__.py
│       ├── bot.py          ← entry point
│       ├── commands/       ← slash command handlers
│       ├── models/         ← Pydantic data models
│       └── data/           ← persistence layer
├── tests/
├── scripts/
│   └── check-rules-updates.sh
├── .claude/                ← Claude Code settings
├── .github/                ← Copilot instructions
├── pyproject.toml
├── .env.example
└── CLAUDE.md               ← you are here
```

## How to Commit

Use Conventional Commits:
- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation only
- `refactor:` — code change that isn't a fix or feature
- `chore:` — tooling, deps, config

## Key Contacts / Context

Discord bot — all user-facing strings must be safe for public Discord servers (no slurs, no NSFW unless the server has that permission level). This is a Crashcart project governed by the AI-rules CEO (Claude in ai-rules repo).
