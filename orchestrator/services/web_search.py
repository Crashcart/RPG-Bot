"""
Web Intel Service — Autonomous Multi-Source Web Searching
==========================================================
Provides fact-grounding for the GM Sandbox and optional GM Director context.

Provider priority
-----------------
1. SerpAPI   — if SERPAPI_KEY is set in config (full web results, ~100 free/month)
2. DuckDuckGo Instant Answers — free, keyless, returns structured abstracts

Usage
-----
    results = await web_search.search("medieval siege weapons")
    # → [{"title": "...", "url": "...", "snippet": "..."}, ...]

Each result dict has:
  title   (str) — page/topic title
  url     (str) — source URL
  snippet (str) — short text excerpt
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from orchestrator.config import Settings

logger = logging.getLogger(__name__)

_DDG_JSON_URL = "https://api.duckduckgo.com/"
_SERPAPI_URL  = "https://serpapi.com/search.json"
_REQUEST_HEADERS = {
    "User-Agent": "IroncladGM/1.0 (world-fact-grounder; +https://github.com/Crashcart/RPG-Bot)"
}


class WebSearchService:
    def __init__(self, settings: Settings) -> None:
        self._serpapi_key = settings.serpapi_key

    # ── Public Interface ──────────────────────────────────────────────────────

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """
        Search the web and return a list of result dicts.

        Returns an empty list on failure (non-fatal — callers degrade gracefully).
        """
        if not query or not query.strip():
            return []
        try:
            if self._serpapi_key:
                return await self._serpapi(query, max_results)
            return await self._duckduckgo(query, max_results)
        except Exception as exc:
            logger.warning("WebSearchService: search failed (non-fatal): %s", exc)
            return []

    async def format_for_prompt(self, query: str, max_results: int = 4) -> str:
        """
        Run a search and format the results as a compact block for LLM injection.

        Returns empty string when no results found.
        """
        results = await self.search(query, max_results)
        if not results:
            return ""
        lines = ["=== WEB SEARCH RESULTS ==="]
        for r in results:
            lines.append(f"[{r['title']}] {r['snippet']}  ({r['url']})")
        lines.append("=== END WEB SEARCH ===")
        return "\n".join(lines)

    # ── SerpAPI Backend ───────────────────────────────────────────────────────

    async def _serpapi(self, query: str, max_results: int) -> list[dict[str, str]]:
        params = {
            "q":      query,
            "api_key": self._serpapi_key,
            "num":    max_results,
            "hl":     "en",
        }
        async with httpx.AsyncClient(timeout=10, headers=_REQUEST_HEADERS) as client:
            resp = await client.get(_SERPAPI_URL, params=params)
            resp.raise_for_status()
        data = resp.json()

        results: list[dict[str, str]] = []
        for r in data.get("organic_results", [])[:max_results]:
            results.append({
                "title":   r.get("title", ""),
                "url":     r.get("link", ""),
                "snippet": r.get("snippet", ""),
            })
        return results

    # ── DuckDuckGo Instant Answers (keyless fallback) ─────────────────────────

    async def _duckduckgo(self, query: str, max_results: int) -> list[dict[str, str]]:
        params = {
            "q":           query,
            "format":      "json",
            "no_redirect": "1",
            "no_html":     "1",
            "skip_disambig": "1",
        }
        async with httpx.AsyncClient(timeout=10, headers=_REQUEST_HEADERS) as client:
            resp = await client.get(_DDG_JSON_URL, params=params)
            resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        results: list[dict[str, str]] = []

        # Primary abstract (single authoritative answer)
        if data.get("AbstractText"):
            results.append({
                "title":   data.get("Heading", query),
                "url":     data.get("AbstractURL", ""),
                "snippet": data["AbstractText"][:300],
            })

        # Related topic snippets
        for topic in data.get("RelatedTopics", []):
            if len(results) >= max_results:
                break
            # RelatedTopics can contain nested sub-topics
            if "Topics" in topic:
                for sub in topic["Topics"]:
                    if len(results) >= max_results:
                        break
                    text = sub.get("Text", "")
                    url  = sub.get("FirstURL", "")
                    if text:
                        title = _extract_title_from_ddg_text(text)
                        results.append({"title": title, "url": url, "snippet": text[:300]})
            else:
                text = topic.get("Text", "")
                url  = topic.get("FirstURL", "")
                if text:
                    title = _extract_title_from_ddg_text(text)
                    results.append({"title": title, "url": url, "snippet": text[:300]})

        return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_title_from_ddg_text(text: str) -> str:
    """DuckDuckGo related topics begin with 'Title - description'. Extract the title."""
    if " - " in text:
        return text.split(" - ")[0].strip()
    return text[:40].strip()
