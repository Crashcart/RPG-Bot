"""
Unit tests for orchestrator/services/web_search.py

Covers:
- WebSearchService.search(): empty/blank query guards, provider routing
- _duckduckgo(): abstract result, related topics, nested sub-topics, max_results cap
- _serpapi(): organic results parsing, max_results cap
- format_for_prompt(): header/footer, empty result, result formatting
- _extract_title_from_ddg_text(): title extraction helper
- Error handling: network failures return [] (non-fatal)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.web_search import (
    WebSearchService,
    _extract_title_from_ddg_text,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_settings(serpapi_key: str = ""):
    s = MagicMock()
    s.serpapi_key = serpapi_key
    return s


def _mock_http_response(data: dict):
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


def _make_async_client(response):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__  = AsyncMock(return_value=False)
    client.get        = AsyncMock(return_value=response)
    return client


# ── TestEmptyQueryGuard ───────────────────────────────────────────────────────

class TestEmptyQueryGuard:
    @pytest.mark.asyncio
    async def test_empty_string_returns_empty(self):
        svc = WebSearchService(_make_settings())
        result = await svc.search("")
        assert result == []

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_empty(self):
        svc = WebSearchService(_make_settings())
        result = await svc.search("   ")
        assert result == []

    @pytest.mark.asyncio
    async def test_none_like_string_still_queries(self):
        """A non-empty, non-blank string must attempt a real search."""
        svc = WebSearchService(_make_settings())
        with patch.object(svc, "_duckduckgo", return_value=[]) as mock_ddg:
            await svc.search("a")
        mock_ddg.assert_called_once()


# ── TestProviderRouting ───────────────────────────────────────────────────────

class TestProviderRouting:
    @pytest.mark.asyncio
    async def test_uses_duckduckgo_when_no_serpapi_key(self):
        svc = WebSearchService(_make_settings(serpapi_key=""))
        with (
            patch.object(svc, "_duckduckgo", return_value=[]) as ddg,
            patch.object(svc, "_serpapi",    return_value=[]) as serp,
        ):
            await svc.search("test query")
        ddg.assert_called_once()
        serp.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_serpapi_when_key_set(self):
        svc = WebSearchService(_make_settings(serpapi_key="abc123"))
        with (
            patch.object(svc, "_serpapi",    return_value=[]) as serp,
            patch.object(svc, "_duckduckgo", return_value=[]) as ddg,
        ):
            await svc.search("test query")
        serp.assert_called_once()
        ddg.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_returns_empty_list(self):
        svc = WebSearchService(_make_settings())
        with patch.object(svc, "_duckduckgo", side_effect=Exception("network error")):
            result = await svc.search("query")
        assert result == []


# ── TestDuckDuckGoSearch ──────────────────────────────────────────────────────

class TestDuckDuckGoSearch:
    def _make_svc(self):
        return WebSearchService(_make_settings(serpapi_key=""))

    @pytest.mark.asyncio
    async def test_abstract_result_returned(self):
        svc = self._make_svc()
        ddg_data = {
            "AbstractText": "Medieval siege engines were used to breach walls.",
            "Heading": "Siege Engine",
            "AbstractURL": "https://example.com/siege",
            "RelatedTopics": [],
        }
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(ddg_data))):
            results = await svc._duckduckgo("siege engines", 5)

        assert len(results) == 1
        assert results[0]["title"] == "Siege Engine"
        assert results[0]["url"] == "https://example.com/siege"
        assert "siege engines" in results[0]["snippet"].lower()

    @pytest.mark.asyncio
    async def test_related_topics_returned(self):
        svc = self._make_svc()
        ddg_data = {
            "AbstractText": "",
            "RelatedTopics": [
                {"Text": "Trebuchet - A counterweight siege engine", "FirstURL": "https://a.com/1"},
                {"Text": "Ballista - A large crossbow weapon",       "FirstURL": "https://a.com/2"},
            ],
        }
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(ddg_data))):
            results = await svc._duckduckgo("siege weapons", 5)

        assert len(results) == 2
        assert results[0]["title"] == "Trebuchet"
        assert results[1]["title"] == "Ballista"

    @pytest.mark.asyncio
    async def test_nested_sub_topics_flattened(self):
        svc = self._make_svc()
        ddg_data = {
            "AbstractText": "",
            "RelatedTopics": [
                {
                    "Topics": [
                        {"Text": "Sub A - First sub-topic", "FirstURL": "https://sub.com/a"},
                        {"Text": "Sub B - Second sub-topic", "FirstURL": "https://sub.com/b"},
                    ]
                }
            ],
        }
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(ddg_data))):
            results = await svc._duckduckgo("topic", 5)

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_max_results_cap_respected(self):
        svc = self._make_svc()
        ddg_data = {
            "AbstractText": "",
            "RelatedTopics": [
                {"Text": f"Topic {i} - description {i}", "FirstURL": f"https://x.com/{i}"}
                for i in range(10)
            ],
        }
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(ddg_data))):
            results = await svc._duckduckgo("query", 3)

        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty(self):
        svc = self._make_svc()
        ddg_data = {"AbstractText": "", "RelatedTopics": []}
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(ddg_data))):
            results = await svc._duckduckgo("nothing", 5)

        assert results == []

    @pytest.mark.asyncio
    async def test_snippet_truncated_to_300_chars(self):
        svc = self._make_svc()
        long_text = "A" * 500
        ddg_data = {
            "AbstractText": long_text,
            "Heading": "Long",
            "AbstractURL": "https://long.com",
            "RelatedTopics": [],
        }
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(ddg_data))):
            results = await svc._duckduckgo("long", 5)

        assert len(results[0]["snippet"]) <= 300

    @pytest.mark.asyncio
    async def test_topics_without_text_skipped(self):
        svc = self._make_svc()
        ddg_data = {
            "AbstractText": "",
            "RelatedTopics": [
                {"Text": "", "FirstURL": "https://empty.com"},  # empty → skip
                {"Text": "Valid - a topic", "FirstURL": "https://valid.com"},
            ],
        }
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(ddg_data))):
            results = await svc._duckduckgo("query", 5)

        assert len(results) == 1
        assert results[0]["title"] == "Valid"


# ── TestSerpAPISearch ─────────────────────────────────────────────────────────

class TestSerpAPISearch:
    def _make_svc(self):
        return WebSearchService(_make_settings(serpapi_key="serp-key"))

    @pytest.mark.asyncio
    async def test_organic_results_parsed(self):
        svc = self._make_svc()
        serp_data = {
            "organic_results": [
                {"title": "Dragon Slaying 101", "link": "https://a.com", "snippet": "How to slay dragons."},
                {"title": "Dragon Scales",      "link": "https://b.com", "snippet": "Armour from scales."},
            ]
        }
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(serp_data))):
            results = await svc._serpapi("dragon", 5)

        assert len(results) == 2
        assert results[0]["title"] == "Dragon Slaying 101"
        assert results[0]["url"]   == "https://a.com"
        assert results[1]["snippet"] == "Armour from scales."

    @pytest.mark.asyncio
    async def test_max_results_cap_applied(self):
        svc = self._make_svc()
        serp_data = {
            "organic_results": [
                {"title": f"Result {i}", "link": f"https://x.com/{i}", "snippet": f"text {i}"}
                for i in range(8)
            ]
        }
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(serp_data))):
            results = await svc._serpapi("query", 3)

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_no_organic_results_returns_empty(self):
        svc = self._make_svc()
        serp_data = {"organic_results": []}
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(serp_data))):
            results = await svc._serpapi("query", 5)

        assert results == []

    @pytest.mark.asyncio
    async def test_missing_organic_results_key_returns_empty(self):
        svc = self._make_svc()
        serp_data = {}  # no organic_results key
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(serp_data))):
            results = await svc._serpapi("query", 5)

        assert results == []

    @pytest.mark.asyncio
    async def test_missing_fields_in_result_use_empty_strings(self):
        svc = self._make_svc()
        serp_data = {"organic_results": [{}]}  # no title/link/snippet
        with patch("orchestrator.services.web_search.httpx.AsyncClient",
                   return_value=_make_async_client(_mock_http_response(serp_data))):
            results = await svc._serpapi("query", 5)

        assert len(results) == 1
        assert results[0]["title"]   == ""
        assert results[0]["url"]     == ""
        assert results[0]["snippet"] == ""


# ── TestFormatForPrompt ───────────────────────────────────────────────────────

class TestFormatForPrompt:
    @pytest.mark.asyncio
    async def test_returns_formatted_block(self):
        svc = WebSearchService(_make_settings())
        results = [
            {"title": "Siege Engine", "url": "https://a.com", "snippet": "Used in medieval wars."},
            {"title": "Catapult",     "url": "https://b.com", "snippet": "A throwing machine."},
        ]
        with patch.object(svc, "search", return_value=results):
            block = await svc.format_for_prompt("siege weapons")

        assert block.startswith("=== WEB SEARCH RESULTS ===")
        assert block.endswith("=== END WEB SEARCH ===")
        assert "Siege Engine" in block
        assert "https://a.com" in block
        assert "Catapult" in block

    @pytest.mark.asyncio
    async def test_no_results_returns_empty_string(self):
        svc = WebSearchService(_make_settings())
        with patch.object(svc, "search", return_value=[]):
            block = await svc.format_for_prompt("nothing found")

        assert block == ""

    @pytest.mark.asyncio
    async def test_passes_max_results_to_search(self):
        svc = WebSearchService(_make_settings())
        with patch.object(svc, "search", return_value=[]) as mock_search:
            await svc.format_for_prompt("query", max_results=2)

        mock_search.assert_called_once_with("query", 2)


# ── TestExtractTitleHelper ────────────────────────────────────────────────────

class TestExtractTitleHelper:
    def test_extracts_title_before_dash(self):
        text = "Trebuchet - A large counterweight siege engine"
        assert _extract_title_from_ddg_text(text) == "Trebuchet"

    def test_handles_multiple_dashes(self):
        text = "A - B - C description here"
        # Only split on the first " - "
        assert _extract_title_from_ddg_text(text) == "A"

    def test_no_dash_truncates_to_40_chars(self):
        text = "This is a topic without any dash separator in the text"
        result = _extract_title_from_ddg_text(text)
        assert len(result) <= 40
        assert result == text[:40].strip()

    def test_empty_string_returns_empty(self):
        assert _extract_title_from_ddg_text("") == ""

    def test_strips_whitespace(self):
        text = "  Goblin  -  A small creature  "
        result = _extract_title_from_ddg_text(text)
        assert result == "Goblin"
