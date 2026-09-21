"""
Tests for orchestrator.services.immersion_filter.ImmersionFilter
"""
import pytest
import asyncio

from orchestrator.services.immersion_filter import ImmersionFilter, _hash_stats


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def f():
    """A fresh ImmersionFilter with no Redis (in-process mode)."""
    return ImmersionFilter()


# ── Brand scrubbing ───────────────────────────────────────────────────────────

class TestScrubBrands:
    def test_detects_single_brand(self, f):
        text, count = f.scrub_brands("The hero ordered a starbucks on the way.")
        assert "[???]" in text
        assert count == 1
        assert "starbucks" not in text.lower()

    def test_case_insensitive(self, f):
        text, count = f.scrub_brands("She checked her DISCORD notifications.")
        assert count == 1
        assert "discord" not in text.lower()

    def test_multiple_brands_one_pass(self, f):
        text, count = f.scrub_brands("He wore Nike shoes and drank Pepsi.")
        assert count == 2
        assert "nike" not in text.lower()
        assert "pepsi" not in text.lower()

    def test_clean_text_unchanged(self, f):
        clean = "The knight raised his sword against the dragon."
        text, count = f.scrub_brands(clean)
        assert count == 0
        assert text == clean

    def test_detect_brand_returns_first_match(self, f):
        found = f.detect_brand("Wearing Nike and Adidas together.")
        # Returns first brand found
        assert found in ("nike", "adidas")

    def test_detect_brand_none_on_clean(self, f):
        assert f.detect_brand("A quiet village by the river.") is None

    def test_replacement_token_is_stable(self, f):
        text, _ = f.scrub_brands("apple and google and amazon")
        assert text.count("[???]") == 3


# ── Structural text stripping ─────────────────────────────────────────────────

class TestStripStructural:
    def test_removes_markdown_heading(self, f):
        text, count = f.strip_structural("## Chapter One\nThe story begins.")
        assert "##" not in text
        assert count >= 1

    def test_removes_divider(self, f):
        text, count = f.strip_structural("Above.\n---\nBelow.")
        assert "---" not in text
        assert count >= 1

    def test_removes_triple_asterisk_divider(self, f):
        text, count = f.strip_structural("Above.\n***\nBelow.")
        assert "***" not in text
        assert count >= 1

    def test_removes_numbered_list_marker(self, f):
        text, count = f.strip_structural("1. First item\n2. Second item")
        assert count >= 1

    def test_removes_bullet_list_marker(self, f):
        text, count = f.strip_structural("- Alpha\n- Beta\n- Gamma")
        assert count >= 1

    def test_collapses_excess_blank_lines(self, f):
        text, _ = f.strip_structural("Para one.\n\n\n\nPara two.")
        assert "\n\n\n" not in text

    def test_prose_untouched(self, f):
        prose = "The lantern flickered as she stepped into the vault."
        text, count = f.strip_structural(prose)
        assert count == 0
        assert text.strip() == prose


# ── Censorship reversion ──────────────────────────────────────────────────────

class TestRevertCensorship:
    def test_reverts_simple_mask(self, f):
        text, count = f.revert_censorship("What the f*** is going on?")
        assert count == 1
        assert "f***" not in text
        assert "f…" in text

    def test_reverts_word_with_tail(self, f):
        text, count = f.revert_censorship("That s**t is broken.")
        assert count == 1
        assert "s**t" not in text
        assert "s…t" in text

    def test_multiple_masks(self, f):
        text, count = f.revert_censorship("F*** that b*****d!")
        assert count == 2

    def test_clean_text_unchanged(self, f):
        clean = "She ran through the forest."
        text, count = f.revert_censorship(clean)
        assert count == 0
        assert text == clean

    def test_single_asterisk_not_matched(self, f):
        text, count = f.revert_censorship("a*b is a footnote.")
        assert count == 0


# ── Markdown list flattening ──────────────────────────────────────────────────

class TestFlattenMarkdownLists:
    def test_flattens_bullet_list(self, f):
        raw = "Ingredients:\n- Sword\n- Shield\n- Potion"
        text, count = f.flatten_markdown_lists(raw)
        assert count >= 1
        assert "- " not in text

    def test_flattens_numbered_list(self, f):
        raw = "Steps:\n1. Enter the dungeon\n2. Defeat the boss\n3. Claim the reward"
        text, count = f.flatten_markdown_lists(raw)
        assert count >= 1
        for i in ("1.", "2.", "3."):
            assert i not in text

    def test_preserves_list_content(self, f):
        raw = "- Dragons\n- Trolls\n- Goblins"
        text, _ = f.flatten_markdown_lists(raw)
        assert "Dragons" in text
        assert "Trolls" in text
        assert "Goblins" in text

    def test_prose_unchanged(self, f):
        prose = "The sky was dark and the wind howled."
        text, count = f.flatten_markdown_lists(prose)
        assert count == 0
        assert prose in text


# ── process() pipeline ────────────────────────────────────────────────────────

class TestProcess:
    def test_chains_all_transforms(self, f):
        raw = (
            "## Setup\n"
            "She wore Nike shoes and ordered Starbucks.\n"
            "What the f*** happened?\n"
            "- Dragons\n- Trolls"
        )
        text, report = f.process(raw)
        # Brand
        assert report["brands_scrubbed"] >= 2
        assert "nike" not in text.lower()
        # Structural
        assert "##" not in text
        # Censorship
        assert "f***" not in text
        # List (content preserved)
        assert "Dragons" in text

    def test_clean_input_zero_report(self, f):
        clean = "The knight stood at the gate, sword drawn."
        text, report = f.process(clean)
        assert text.strip() == clean
        assert report["brands_scrubbed"] == 0
        assert report["structural_stripped"] == 0
        assert report["censorship_reverted"] == 0
        assert report["lists_flattened"] == 0


# ── Sheet-diff gate ───────────────────────────────────────────────────────────

class TestShouldRenderSheet:
    def test_first_call_always_true(self, f):
        assert f.should_render_sheet_sync("player_1", {"hp": 10, "mp": 5})

    def test_unchanged_stats_returns_false(self, f):
        stats = {"hp": 10, "mp": 5}
        f.should_render_sheet_sync("player_2", stats)
        assert not f.should_render_sheet_sync("player_2", stats)

    def test_changed_stat_returns_true(self, f):
        f.should_render_sheet_sync("player_3", {"hp": 10})
        assert f.should_render_sheet_sync("player_3", {"hp": 7})

    def test_key_order_does_not_affect_hash(self, f):
        f.should_render_sheet_sync("player_4", {"hp": 10, "mp": 5})
        # Same stats, different key order
        assert not f.should_render_sheet_sync("player_4", {"mp": 5, "hp": 10})

    def test_different_players_independent(self, f):
        stats_a = {"hp": 10}
        stats_b = {"hp": 10}
        f.should_render_sheet_sync("player_a", stats_a)
        f.should_render_sheet_sync("player_b", stats_b)
        # Change A only
        assert f.should_render_sheet_sync("player_a", {"hp": 8})
        assert not f.should_render_sheet_sync("player_b", stats_b)

    def test_invalidate_forces_render(self, f):
        stats = {"hp": 10}
        f.should_render_sheet_sync("player_5", stats)
        f.invalidate_sheet_cache("player_5")
        assert f.should_render_sheet_sync("player_5", stats)

    @pytest.mark.asyncio
    async def test_async_variant_first_call_true(self):
        filt = ImmersionFilter()
        result = await filt.should_render_sheet("player_async", {"hp": 10})
        assert result is True

    @pytest.mark.asyncio
    async def test_async_variant_no_change_false(self):
        filt = ImmersionFilter()
        stats = {"hp": 10, "mp": 3}
        await filt.should_render_sheet("player_async2", stats)
        result = await filt.should_render_sheet("player_async2", stats)
        assert result is False


# ── _hash_stats utility ───────────────────────────────────────────────────────

class TestHashStats:
    def test_same_dict_same_hash(self):
        h1 = _hash_stats({"hp": 10, "mp": 5})
        h2 = _hash_stats({"hp": 10, "mp": 5})
        assert h1 == h2

    def test_different_values_different_hash(self):
        h1 = _hash_stats({"hp": 10})
        h2 = _hash_stats({"hp": 9})
        assert h1 != h2

    def test_key_order_irrelevant(self):
        h1 = _hash_stats({"a": 1, "b": 2})
        h2 = _hash_stats({"b": 2, "a": 1})
        assert h1 == h2
