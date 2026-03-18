"""
Ironclad GM – Story Memory Service
=====================================
Gives the GM a persistent, queryable record of every established world
fact so it can never hallucinate contradictions.

Two operations:

1. retrieve_relevant_context(query, campaign_id)
   ─ Semantic search over the story_context ChromaDB collection for facts
     relevant to the current player action.
   ─ Also pulls the N most-recently established facts as a recency anchor
     (so recent plot beats are always present regardless of relevance score).

2. extract_and_store(narrative, campaign_id, intent_id)
   ─ Calls Gemini with a strict extraction prompt to identify new entities
     and facts introduced by the generated narrative.
   ─ Upserts each fact into PostgreSQL story_context and re-embeds it in
     ChromaDB so future lookups stay accurate.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import chromadb
import httpx

from orchestrator.config import Settings
from orchestrator.schemas.payloads import (
    ExtractedFact,
    ExtractionResult,
    StoryEntityType,
    StoryFact,
)

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
You are a structured data extractor for a tabletop RPG campaign journal.

You will be given a narrative passage. Extract every NEW world fact, NPC, \
location, event, or plot thread that is EXPLICITLY established in the text. \
Do not infer or speculate beyond what is stated.

Output ONLY valid JSON matching this schema exactly:
{
  "facts": [
    {
      "entity_type": "<npc|location|event|world_fact|plot_thread>",
      "entity_name": "<canonical short name>",
      "summary":     "<one sentence: what is true about this entity>",
      "detail":      "<optional extended context, max 300 chars>"
    }
  ]
}

Rules:
- If the same entity appears multiple times, merge into one entry.
- Do not include facts that were provided as prior context.
- Do not include mechanical numbers (HP, dice results).
- If nothing new is established, return {"facts": []}.

NARRATIVE:
{narrative}
"""

_CHROMA_COLLECTION = "story_memory_{campaign_id}"


class StoryMemoryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._gemini_api_key = settings.gemini_api_key
        self._gemini_model   = settings.gemini_model
        self._chroma_host    = settings.chroma_host
        self._chroma_port    = settings.chroma_port
        self._chroma: chromadb.AsyncHttpClient | None = None
        self._pool: asyncpg.Pool | None = None

    async def connect(self, pool: asyncpg.Pool) -> None:
        """Attach the existing DB pool and open the ChromaDB client."""
        self._pool = pool
        self._chroma = await chromadb.AsyncHttpClient(
            host=self._chroma_host,
            port=self._chroma_port,
        )
        logger.info("StoryMemoryService connected.")

    # ── Public API ────────────────────────────────────────────────────────────

    async def retrieve_relevant_context(
        self,
        query: str,
        campaign_id: str,
        n_semantic: int = 8,
        n_recent: int = 5,
    ) -> list[StoryFact]:
        """
        Return a deduplicated list of story facts most relevant to the query.
        Combines semantic (ChromaDB) and recency (PostgreSQL ORDER BY) results.
        """
        semantic = await self._semantic_search(query, campaign_id, n_semantic)
        recent   = await self._recent_facts(campaign_id, n_recent)

        # Deduplicate by fact_id, semantic results rank first
        seen: set[str] = set()
        combined: list[StoryFact] = []
        for fact in semantic + recent:
            if fact.fact_id not in seen:
                seen.add(fact.fact_id)
                combined.append(fact)

        logger.debug(
            "Story context: %d semantic + %d recent → %d unique facts for campaign %s",
            len(semantic), len(recent), len(combined), campaign_id,
        )
        return combined

    async def extract_and_store(
        self,
        narrative: str,
        campaign_id: str,
        intent_id: str,
    ) -> list[StoryFact]:
        """
        Extract new world facts from the generated narrative and persist them.
        Returns the list of newly upserted facts.
        """
        extracted = await self._call_gemini_extractor(narrative)
        if not extracted.facts:
            return []

        stored: list[StoryFact] = []
        for ef in extracted.facts:
            fact = await self._upsert_fact(ef, campaign_id, intent_id)
            if fact:
                stored.append(fact)

        logger.info(
            "Extracted and stored %d story facts for campaign %s",
            len(stored), campaign_id,
        )
        return stored

    # ── Semantic Search ───────────────────────────────────────────────────────

    async def _semantic_search(
        self, query: str, campaign_id: str, n: int
    ) -> list[StoryFact]:
        if not self._chroma:
            return []
        collection_name = _CHROMA_COLLECTION.format(campaign_id=campaign_id[:8])
        try:
            collection = await self._chroma.get_collection(collection_name)
            results = await collection.query(
                query_texts=[query],
                n_results=n,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            # Collection may not exist yet for a new campaign
            return []

        facts = []
        docs      = results.get("documents",  [[]])[0]
        metas     = results.get("metadatas",  [[]])[0]
        distances = results.get("distances",  [[]])[0]

        for doc, meta, dist in zip(docs, metas, distances):
            facts.append(StoryFact(
                fact_id=meta.get("fact_id", str(uuid.uuid4())),
                entity_type=StoryEntityType(meta.get("entity_type", "world_fact")),
                entity_name=meta.get("entity_name", "unknown"),
                summary=meta.get("summary", doc[:200]),
                detail=doc,
                relevance=round(max(0.0, 1.0 - dist), 4),
                established_at=datetime.fromisoformat(
                    meta.get("established_at", datetime.now(timezone.utc).isoformat())
                ),
            ))
        return facts

    # ── Recency Query ─────────────────────────────────────────────────────────

    async def _recent_facts(self, campaign_id: str, n: int) -> list[StoryFact]:
        if not self._pool:
            return []
        rows = await self._pool.fetch(
            """
            SELECT id, entity_type, entity_name, summary, detail, last_updated_at
            FROM story_context
            WHERE campaign_id = $1
            ORDER BY last_updated_at DESC
            LIMIT $2
            """,
            uuid.UUID(campaign_id),
            n,
        )
        return [
            StoryFact(
                fact_id=str(r["id"]),
                entity_type=StoryEntityType(r["entity_type"]),
                entity_name=r["entity_name"],
                summary=r["summary"],
                detail=r["detail"] or "",
                relevance=1.0,
                established_at=r["last_updated_at"],
            )
            for r in rows
        ]

    # ── Gemini Extraction Call ────────────────────────────────────────────────

    async def _call_gemini_extractor(self, narrative: str) -> ExtractionResult:
        prompt = _EXTRACTION_PROMPT.format(narrative=narrative[:3000])
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,   # near-deterministic for extraction
                "maxOutputTokens": 1024,
                "responseMimeType": "application/json",
            },
        }
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._gemini_model}:generateContent?key={self._gemini_api_key}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            raw = response.json()
            text = raw["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            return ExtractionResult(**data)
        except Exception as exc:
            logger.warning("Fact extraction failed: %s", exc)
            return ExtractionResult(facts=[])

    # ── DB Upsert ─────────────────────────────────────────────────────────────

    async def _upsert_fact(
        self, ef: ExtractedFact, campaign_id: str, intent_id: str
    ) -> StoryFact | None:
        if not self._pool:
            return None

        doc_id = str(uuid.uuid4())
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO story_context
                    (campaign_id, entity_type, entity_name, summary, detail,
                     chroma_doc_id, source_intent_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (campaign_id, entity_name) DO UPDATE
                    SET summary         = EXCLUDED.summary,
                        detail          = EXCLUDED.detail,
                        last_updated_at = NOW(),
                        source_intent_id = EXCLUDED.source_intent_id
                RETURNING id, last_updated_at
                """,
                uuid.UUID(campaign_id),
                ef.entity_type.value,
                ef.entity_name,
                ef.summary,
                ef.detail,
                doc_id,
                uuid.UUID(intent_id) if intent_id else None,
            )
        except Exception as exc:
            logger.warning("story_context upsert failed for '%s': %s", ef.entity_name, exc)
            return None

        # Keep ChromaDB in sync
        await self._embed_fact(
            fact_id=str(row["id"]),
            doc_id=doc_id,
            ef=ef,
            campaign_id=campaign_id,
            established_at=row["last_updated_at"],
        )

        return StoryFact(
            fact_id=str(row["id"]),
            entity_type=ef.entity_type,
            entity_name=ef.entity_name,
            summary=ef.summary,
            detail=ef.detail,
            relevance=1.0,
            established_at=row["last_updated_at"],
        )

    async def _embed_fact(
        self,
        fact_id: str,
        doc_id: str,
        ef: ExtractedFact,
        campaign_id: str,
        established_at: datetime,
    ) -> None:
        if not self._chroma:
            return
        collection_name = _CHROMA_COLLECTION.format(campaign_id=campaign_id[:8])
        try:
            collection = await self._chroma.get_or_create_collection(collection_name)
            # Upsert by doc_id so re-runs update rather than duplicate
            await collection.upsert(
                ids=[doc_id],
                documents=[f"{ef.entity_name}: {ef.summary}. {ef.detail}"],
                metadatas=[{
                    "fact_id":       fact_id,
                    "entity_type":   ef.entity_type.value,
                    "entity_name":   ef.entity_name,
                    "summary":       ef.summary[:200],
                    "established_at": established_at.isoformat(),
                }],
            )
        except Exception as exc:
            logger.warning("ChromaDB embed failed for '%s': %s", ef.entity_name, exc)
