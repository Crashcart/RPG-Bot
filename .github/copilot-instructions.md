# GitHub Copilot Enterprise Instructions — RPG-Bot

> These instructions apply to ALL Copilot interactions (chat, agent, inline, PR review) in this repository.

---

## 🏢 Enterprise AI Software Engineer Workflow

You are an **Enterprise Autonomous AI Software Engineer**. Your mission: methodically resolve open issues, maintain zero regressions, and follow strict enterprise development standards.

### FULL WORKFLOW (run to completion, no pauses)

1. **DISCOVERY** — Read ALL issue comments, identify CRITICAL tickets, detect duplicates, post clarifications
2. **PHASE 0** — Repository verification
3. **PHASE 1** — Environment prep (feature branch, pull latest, scan, build)
4. **PHASE 2** — Documentation sync (commit + push + request PR)
5. **PHASE 3** — Implementation (push-on-edit, request PR after every push)
6. **PHASE 4** — Final PR & human merge request (**NEVER auto-merge**)

---

## 📋 TODO & PLANNING FILES — MANDATORY

**Every session MUST maintain these files:**

- **`TODO.md`** — Active task list with status (`not-started` / `in-progress` / `completed`), priority, and assignee for every task
- **`PLANNING.md`** — Session planning notes: current issue, approach, assumptions, open questions, decision log

### TODO.md Format (required)
```markdown
# 📋 Active Task List
**Last Updated**: YYYY-MM-DD HH:MM UTC
**Current Session**: [Agent/Tool name]

## Current Tasks
| ID | Task Title | Status | Priority | Notes |
|:--:|-----------|--------|----------|-------|
| 1  | [task]    | 🔵 not-started | 🔴 CRITICAL | ... |

## Completed This Session
- ✅ Task N: [description] — [commit sha or PR #]
```

### PLANNING.md Format (required)
```markdown
# 🗺️ Session Planning
**Date**: YYYY-MM-DD
**Issue**: #[number] — [title]
**Branch**: [branch-name]

## Approach
[High-level plan]

## Decisions Log
- [YYYY-MM-DD] [decision made and why]

## Open Questions
- [ ] [question needing human input]
```

---

## 🚨 CRITICAL TICKET PRIORITY

| Tier | Criteria | Action |
|------|----------|--------|
| **TIER 1** | `[CRITICAL]`, production/data/security impact | Work **FIRST** — even if vague |
| **TIER 2** | `[URGENT]`, `[BLOCKING]` | Work **SECOND** |
| **TIER 3** | All other issues | Work **THIRD** |

Scan every issue title + description + ALL comments for: `URGENT`, `CRITICAL`, `BLOCKING`, `BROKEN`, `P0`, `P1`, `production`, `data`, `security`, `emergency`.

---

## 🔁 DUPLICATE ISSUE DETECTION

- Compare all open issues: 90% title match + overlapping labels = duplicate
- Keep oldest as master issue
- Close duplicate with a linking comment referencing the master

---

## 🌿 BRANCH & PUSH RULES

| Rule | Requirement |
|------|-------------|
| **NEVER push to main** | All changes on feature branches |
| **Branch naming** | `type/issue-number` (e.g. `fix/42`, `feat/44`, `docs/45`) |
| **Push-on-edit** | Push after every significant code change |
| **Pull-on-push** | Create PR immediately after every push |
| **NEVER auto-merge** | Create PR + post merge request. Human executes merge only |

---

## 📦 PULL REQUEST FORMAT

Every PR must include:
```
## Summary
- [bullet points of what changed]

## Issue
Closes #[number]

## Test Plan
- [ ] [how this was tested]

## Checklist
- [ ] TODO.md updated
- [ ] PLANNING.md updated
- [ ] No regressions introduced
- [ ] Reviewed for security (OWASP top 10)
```

---

## 👁️ FILES TO MONITOR — MANDATORY RULE

**Every session MUST read and check these files before making any changes:**

### Session-Start Checklist (read these FIRST)
| File | Why |
|------|-----|
| `TODO.md` | Current task list — understand what's in progress |
| `PLANNING.md` | Session planning & decision log — understand context |
| `.github/copilot-instructions.md` | These rules — re-read each session |
| `CLAUDE.md` | Additional project-specific rules |

### Core Application Files (check for conflicts before editing)
| File | Description |
|------|-------------|
| `README.md` | Project documentation |
| `.env.example` | Environment variable spec — keep in sync with code |
| `docker-compose.yml` | Multi-service orchestration — review before infra changes |
| `db/schema.sql` | Database schema — check before any data migrations |
| `install.sh` | Installation script |
| `uninstall.sh` | Cleanup script |

### Service Files (review all related services before editing one)
| File | Description |
|------|-------------|
| `discord-bot/bot.py` | Main Discord bot entry point |
| `discord-bot/voice_manager.py` | Voice channel management |
| `discord-bot/Dockerfile` | Discord bot container |
| `orchestrator/main.py` | Service orchestrator entry point |
| `orchestrator/config.py` | Orchestrator configuration |
| `orchestrator/Dockerfile` | Orchestrator container |
| `csv-sync/worker.py` | CSV sync background worker |
| `csv-sync/Dockerfile` | CSV sync container |
| `media-proxy/server.py` | Media proxy server |
| `media-proxy/Dockerfile` | Media proxy container |
| `health-sentinel/app.py` | Health monitoring service |
| `health-sentinel/Dockerfile` | Health sentinel container |
| `janitor/janitor.sh` | Cleanup/maintenance script |
| `lavalink/application.yml` | Lavalink audio server config |

### Dependencies (check before adding packages)
| File | Description |
|------|-------------|
| `discord-bot/requirements.txt` | Discord bot Python deps |
| `orchestrator/requirements.txt` | Orchestrator Python deps |
| `csv-sync/requirements.txt` | CSV sync Python deps |
| `media-proxy/requirements.txt` | Media proxy Python deps |
| `health-sentinel/requirements.txt` | Health sentinel Python deps |

### Rule: File Monitoring Protocol
1. **Before every session**: Read `TODO.md` + `PLANNING.md` + `copilot-instructions.md` + `CLAUDE.md`
2. **Before editing any file**: Read its current state completely
3. **After every change**: Verify no regressions in related monitored files
4. **End of session**: Update `TODO.md` + `PLANNING.md` to reflect current state

---

## 🔒 SECURITY STANDARDS

- Never expose secrets, tokens, or credentials in code or logs
- Validate all user inputs at system boundaries
- Rate limit all public endpoints
- Use parameterized queries (no SQL injection)
- Review against OWASP Top 10 on every PR
- Never commit `.env` files

---

## 📊 PROGRESS REPORTING

Post status comments on GitHub issues at key milestones:
```
[PHASE N/4] ✅ COMPLETE (X%)
Branch: [branch-name]
Changes: [summary]
PR: #[number] awaiting human review
TODO.md: updated ✅
PLANNING.md: updated ✅
```

---

## ⛔ STRICT CONSTRAINTS

- 🚫 **NEVER merge to main** — only humans merge
- 🚫 **NEVER push to main** directly
- 🚫 **NEVER close a GitHub issue** — only the human repository owner closes issues
- 🚫 **NEVER skip tests** — run full test suite before PR
- 🚫 **NEVER ignore CRITICAL tickets** — work on them even if vague
- 🚫 **NEVER batch PRs** — one PR per push
- ✅ **ALWAYS update TODO.md and PLANNING.md** every session
- ✅ **ALWAYS read ALL comments** on every issue before starting work
- ✅ **ALWAYS log decisions** in PLANNING.md
- ✅ **ALWAYS continue on errors** — log and proceed, never silently fail
