## Code Quality Rules

- All new Python files must pass `ruff check` with zero errors.
- No `print()` statements — use `logging.getLogger(__name__)`.
- No bare `except:` clauses — always catch a specific exception type.
- Type hints required on all public function signatures.
- Async functions must use `await` for all I/O — no blocking calls in coroutines.
- Do not add backwards-compatibility shims for removed code. Delete cleanly.
- Comments only where the WHY is non-obvious — do not comment what the code does.
- New services must be exported from `orchestrator/services/__init__.py`.
