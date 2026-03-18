"""ChromaDB RAG service for rulebook retrieval."""

from __future__ import annotations

import logging
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from orchestrator.config import Settings
from orchestrator.schemas.payloads import RuleChunk

logger = logging.getLogger(__name__)


class RAGService:
    def __init__(self, settings: Settings) -> None:
        self._host = settings.chroma_host
        self._port = settings.chroma_port
        self._client: chromadb.AsyncHttpClient | None = None

    async def connect(self) -> None:
        self._client = await chromadb.AsyncHttpClient(
            host=self._host,
            port=self._port,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        logger.info("ChromaDB client connected.")

    @property
    def client(self) -> chromadb.AsyncHttpClient:
        if self._client is None:
            raise RuntimeError("RAGService not connected.")
        return self._client

    async def retrieve_rule_chunks(
        self,
        query: str,
        collection_names: list[str],
        n_results: int = 5,
    ) -> list[RuleChunk]:
        """
        Query one or more ChromaDB collections and return the top-N
        relevant rulebook chunks for the given player action query.
        """
        chunks: list[RuleChunk] = []

        for collection_name in collection_names:
            try:
                collection = await self.client.get_collection(collection_name)
                results = await collection.query(
                    query_texts=[query],
                    n_results=n_results,
                    include=["documents", "metadatas", "distances"],
                )
            except Exception as exc:
                logger.warning("RAG query failed for collection %s: %s", collection_name, exc)
                continue

            docs      = results.get("documents",  [[]])[0]
            metas     = results.get("metadatas",  [[]])[0]
            distances = results.get("distances",  [[]])[0]

            for doc, meta, dist in zip(docs, metas, distances):
                # ChromaDB returns L2 distance; convert to 0-1 relevance score
                relevance = max(0.0, 1.0 - dist)
                chunks.append(
                    RuleChunk(
                        chunk_id=meta.get("chunk_id", "unknown"),
                        source=meta.get("source", collection_name),
                        content=doc,
                        relevance=round(relevance, 4),
                    )
                )

        # Sort by relevance descending, return top N across all collections
        chunks.sort(key=lambda c: c.relevance, reverse=True)
        return chunks[:n_results]

    async def ingest_document(
        self,
        collection_name: str,
        chunks: list[dict[str, Any]],
    ) -> int:
        """
        Ingest pre-chunked rulebook text into a ChromaDB collection.
        chunks: list of {"id": str, "text": str, "source": str}
        Returns the number of chunks ingested.
        """
        collection = await self.client.get_or_create_collection(collection_name)
        await collection.add(
            ids=[c["id"] for c in chunks],
            documents=[c["text"] for c in chunks],
            metadatas=[{"source": c["source"], "chunk_id": c["id"]} for c in chunks],
        )
        logger.info("Ingested %d chunks into collection '%s'.", len(chunks), collection_name)
        return len(chunks)
