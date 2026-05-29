## Database Rules

- New migrations go in `db/migrations/0NN_<snake_case>.sql`. The `NN` must be the next sequential
  number. **Never modify an existing migration file.**
- Schema changes require a migration. Never ALTER TABLE in application code.
- `global_settings` / `system_settings` seeds belong in the migration that introduces the feature.
- Always use asyncpg via `DatabaseService` — never a raw connection string in application code.
- New env vars declared in `orchestrator/config.py` must also appear in `.env.example`.
