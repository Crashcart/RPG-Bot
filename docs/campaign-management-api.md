# Campaign Management REST API

Provides CRUD endpoints for managing campaigns. Multiple campaigns can co-exist
in the same Discord guild; only one is *active* at a time (the most recently
created or activated campaign is used for pipeline actions in that guild).

---

## Endpoints

### List campaigns

```
GET /api/campaigns
```

Query parameters:
- `guild_id` *(optional)* – filter to a specific Discord guild. When omitted,
  returns all **active** campaigns across all guilds. When provided, returns
  **all** campaigns for that guild (including inactive ones) so admins can see
  the full campaign history.

**Response** `200 OK` — array of campaign objects (see schema below).

---

### Create a campaign

```
POST /api/campaigns
```

**Body**

```json
{
  "guild_id":  "987654321098765432",
  "name":      "The Sunken City",
  "system":    "Mothership",
  "settings":  { "house_rules": ["no luck re-rolls"] }
}
```

| Field      | Required | Description |
|------------|----------|-------------|
| `guild_id` | ✅        | Discord server snowflake |
| `name`     | ✅        | Display name (1–120 chars); must be unique per guild |
| `system`   | ✅        | TTRPG system name (1–80 chars) |
| `settings` |          | Campaign-level rule overrides (JSONB) — defaults to `{}` |

**Response** `201 Created` — the created campaign object.

**Errors**
- `409 Conflict` — a campaign with that name already exists in the guild.

---

### Get a campaign

```
GET /api/campaigns/{campaign_id}
```

**Response** `200 OK` — single campaign object (includes inactive campaigns).

**Errors**
- `404 Not Found`

---

### Update a campaign

```
PATCH /api/campaigns/{campaign_id}
```

Partial update — only the fields supplied in the body are changed.

**Body** (all optional, at least one required):

```json
{
  "name":     "The Sunken City – Act II",
  "system":   "Mothership 2e",
  "settings": { "house_rules": [] }
}
```

**Response** `200 OK` — the updated campaign object.

**Errors**
- `400 Bad Request` — no fields provided.
- `404 Not Found`

---

### Deactivate (soft-delete) a campaign

```
DELETE /api/campaigns/{campaign_id}
```

Sets `active = FALSE`. All related data (characters, inventories, action_log,
story facts) is preserved. The campaign is excluded from future pipeline
actions until reactivated via a direct database update.

**Response** `200 OK`

```json
{ "status": "deactivated", "campaign_id": "..." }
```

**Errors**
- `404 Not Found` — campaign not found or already inactive.

---

## Campaign Object Schema

```json
{
  "id":              "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "guild_id":        "987654321098765432",
  "name":            "The Sunken City",
  "system":          "Mothership",
  "active":          true,
  "settings":        {},
  "character_count": 3,
  "fact_count":      12,
  "created_at":      "2026-01-15T20:30:00Z"
}
```

| Field             | Type    | Description |
|-------------------|---------|-------------|
| `id`              | UUID    | Primary key |
| `guild_id`        | string  | Discord server snowflake |
| `name`            | string  | Display name |
| `system`          | string  | TTRPG system |
| `active`          | boolean | False = soft-deleted |
| `settings`        | object  | Campaign-level JSONB rule overrides |
| `character_count` | int     | Number of `ALIVE` characters |
| `fact_count`      | int     | Number of story_context rows |
| `created_at`      | ISO8601 | Row creation timestamp |

---

## Database Methods Added

All new methods live in `orchestrator/services/database.py`:

| Method | Description |
|--------|-------------|
| `create_campaign(guild_id, name, system, settings)` | Insert a new campaign row |
| `get_campaign_by_id(campaign_id)` | Fetch one campaign by UUID (includes inactive) |
| `list_campaigns_by_guild(guild_id)` | All campaigns for a guild, newest first |
| `update_campaign(campaign_id, name, system, settings)` | Partial update; returns `None` if not found |
| `deactivate_campaign(campaign_id)` | Soft-delete; returns `True` if a row was updated |

---

## Running the Tests

```bash
pip install pytest pytest-asyncio httpx
pytest orchestrator/tests/test_campaign_management.py -v
```

The suite contains 30 tests (12 schema + DB unit tests, 12 API endpoint tests)
and requires no live database or Redis instance.
