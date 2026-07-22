## Naming and Branding Rules

- **In-world names must come from ingested PDF rulebooks** retrieved via RAG from ChromaDB.
  Names that appear in the active campaign's rulebooks are allowed in generated narrative.
- **Real-world brand names are blocked** unless the name appears in both the real world AND in an
  ingested rulebook (e.g. a licensed product that canonically uses real brand names).
  When in doubt, use a generic equivalent — never invent a real-world brand reference.
- The sub-agent post-processor applies brand filtering to all `SubAgentResult` output.
  If filtering fails after retry, `brand_violation=True` is set — do not suppress this flag.
- Do not introduce new proper nouns, weapon names, faction names, or lore terms that are not
  sourced from the campaign's rulebooks or the `story_facts` / `story_entities` tables.
- NPC names, location names, and item names generated during narration must be consistent with
  the active world's `narrative_tone` and genre (read from `WorldRegistry`).
