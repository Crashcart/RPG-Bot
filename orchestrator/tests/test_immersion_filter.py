"""
Unit tests for ImmersionFilter.

Covers Pass 1 (censorship reversion), Pass 2 (markdown flattening),
Pass 3 (brand nullification), character-sheet UI gate, and FilterReport.
"""

from __future__ import annotations

import pytest

from orchestrator.services.immersion_filter import FilterReport, ImmersionFilter


@pytest.fixture
def fltr() -> ImmersionFilter:
    return ImmersionFilter()


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1 — Censorship Reversion
# ─────────────────────────────────────────────────────────────────────────────

class TestCensorshipReversion:
    def test_fuck_pattern(self, fltr):
        out, report = fltr.scrub_narrative("He yelled 'f**k that' at the guards.")
        assert "fuck" in out
        assert "*" not in out
        assert report.censorship_reversions >= 1

    def test_shit_pattern(self, fltr):
        out, _ = fltr.scrub_narrative("'sh*t,' she muttered under her breath.")
        assert "shit" in out

    def test_bitch_pattern(self, fltr):
        out, _ = fltr.scrub_narrative("The b*tch snarled and drew her blade.")
        assert "bitch" in out

    def test_bastard_pattern(self, fltr):
        out, _ = fltr.scrub_narrative("You filthy b*stard!")
        assert "bastard" in out

    def test_bloody_pattern(self, fltr):
        out, _ = fltr.scrub_narrative("The bl**dy corpse lay sprawled on the stones.")
        assert "bloody" in out

    def test_fallback_strips_unknown_asterisk(self, fltr):
        out, report = fltr.scrub_narrative("The g*r*m creature lunged.")
        assert "*" not in out
        assert report.censorship_reversions >= 1

    def test_clean_text_untouched(self, fltr):
        text = "The knight strode into the tavern without a word."
        out, report = fltr.scrub_narrative(text)
        assert out == text
        assert report.censorship_reversions == 0


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2 — Markdown List / Table Flattening
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkdownFlattening:
    def test_bullet_list_flattened_to_prose(self, fltr):
        text = (
            "The room contains:\n"
            "- a broken sword\n"
            "- a pile of bones\n"
            "- a rusted lock\n"
        )
        out, report = fltr.scrub_narrative(text)
        assert "-" not in out
        assert "broken sword" in out
        assert "pile of bones" in out
        assert report.list_flattenings >= 1

    def test_numbered_list_flattened(self, fltr):
        text = (
            "Three things catch your eye:\n"
            "1. A glowing rune\n"
            "2. A pool of blood\n"
            "3. A locked chest\n"
        )
        out, report = fltr.scrub_narrative(text)
        assert "1." not in out and "2." not in out and "3." not in out
        assert "glowing rune" in out
        assert report.list_flattenings >= 1

    def test_single_bullet_not_flattened(self, fltr):
        """A single bullet item is below the 2-item threshold — leave unchanged."""
        text = "The guard says:\n- Move along.\n"
        _, report = fltr.scrub_narrative(text)
        assert report.list_flattenings == 0

    def test_markdown_table_flattened(self, fltr):
        text = (
            "| Name   | HP  |\n"
            "|--------|-----|\n"
            "| Goblin | 12  |\n"
            "| Troll  | 40  |\n"
        )
        out, report = fltr.scrub_narrative(text)
        assert "|" not in out
        assert "Goblin" in out
        assert report.list_flattenings >= 1

    def test_prose_unchanged_by_pass2(self, fltr):
        text = "The corridor stretches ahead, dark and utterly silent."
        out, report = fltr.scrub_narrative(text)
        assert out == text
        assert report.list_flattenings == 0


# ─────────────────────────────────────────────────────────────────────────────
# Pass 3 — Brand Nullification
# ─────────────────────────────────────────────────────────────────────────────

class TestBrandNullification:
    def test_known_brand_replaced(self, fltr):
        out, report = fltr.scrub_narrative("He handed you a Starbucks cup.")
        assert "Starbucks" not in out
        assert report.brand_nullifications >= 1

    def test_brand_case_insensitive(self, fltr):
        out, report = fltr.scrub_narrative("It tasted like COCA-COLA.")
        assert "COCA-COLA" not in out
        assert report.brand_nullifications >= 1

    def test_lore_substitution_applied(self, fltr):
        out, _ = fltr.scrub_narrative("She ordered a Starbucks.")
        assert "Amber Leaf" in out

    def test_unknown_brand_gets_sentinel(self, fltr):
        fltr2 = ImmersionFilter(extra_blocklist=["OmegaCorp"])
        out, report = fltr2.scrub_narrative("The OmegaCorp logo gleamed.")
        assert "OmegaCorp" not in out
        assert "[???]" in out
        assert report.brand_nullifications >= 1

    def test_extra_blocklist_respected(self):
        fltr2 = ImmersionFilter(extra_blocklist=["ShadowTech"])
        out, report = fltr2.scrub_narrative("ShadowTech owns the city.")
        assert "ShadowTech" not in out
        assert report.brand_nullifications >= 1

    def test_no_false_positives_on_fantasy_names(self, fltr):
        text = "Arakar the Undying raised his ancient staff."
        out, report = fltr.scrub_narrative(text)
        assert out == text
        assert report.brand_nullifications == 0


# ─────────────────────────────────────────────────────────────────────────────
# Character-Sheet UI Gate
# ─────────────────────────────────────────────────────────────────────────────

class TestCharacterSheetGate:
    def test_identical_states_returns_false(self, fltr):
        state = {"hp": 10, "mp": 5, "gold": 100}
        assert fltr.should_render_character_sheet(state, state) is False

    def test_changed_hp_returns_true(self, fltr):
        pre  = {"hp": 10, "mp": 5}
        post = {"hp": 7,  "mp": 5}
        assert fltr.should_render_character_sheet(pre, post) is True

    def test_changed_inventory_returns_true(self, fltr):
        pre  = {"gold": 100, "items": ["sword"]}
        post = {"gold": 90,  "items": ["sword", "potion"]}
        assert fltr.should_render_character_sheet(pre, post) is True

    def test_hash_is_deterministic(self, fltr):
        state = {"a": 1, "b": [2, 3]}
        assert fltr.compute_state_hash(state) == fltr.compute_state_hash(state)

    def test_hash_is_key_order_invariant(self, fltr):
        s1 = {"z": 9, "a": 1}
        s2 = {"a": 1, "z": 9}
        assert fltr.compute_state_hash(s1) == fltr.compute_state_hash(s2)

    def test_hash_differs_for_different_values(self, fltr):
        s1 = {"hp": 10}
        s2 = {"hp": 9}
        assert fltr.compute_state_hash(s1) != fltr.compute_state_hash(s2)


# ─────────────────────────────────────────────────────────────────────────────
# FilterReport
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterReport:
    def test_any_applied_true_when_changes(self):
        assert FilterReport(censorship_reversions=1).any_applied() is True

    def test_any_applied_false_when_clean(self):
        assert FilterReport().any_applied() is False

    def test_as_dict_has_correct_keys(self):
        d = FilterReport(list_flattenings=2, brand_nullifications=1).as_dict()
        assert set(d.keys()) == {"censorship_reversions", "list_flattenings", "brand_nullifications"}
        assert d["list_flattenings"] == 2
        assert d["brand_nullifications"] == 1
