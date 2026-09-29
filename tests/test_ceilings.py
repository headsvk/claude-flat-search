"""Ceilings: rules that keep a listing out of High however many bonuses stack.

A one-step demotion is cancelled by any one bonus. On 2026-09-28, with
furnishing costing a tier, 193 of 462 High listings were fully furnished.
"""
import unittest

from helpers import make_config
from flatsearch import core

PRIME_CONCIERGE = {"postcode": "NW8 7AA", "concierge": "yes",
                   "furnished": "Unfurnished", "floor_number": 5, "lift": "yes"}


def rank(listing, cfg):
    tier, _ = core.base_tier(listing, cfg)
    tier, _ = core.apply_amenities(tier, listing, cfg)
    tier, notes = core.apply_ceilings(tier, listing, cfg)
    return core.TIER_NAME[tier], notes


class TestFurnishingStrict(unittest.TestCase):
    def test_furnished_with_every_bonus_is_never_high(self):
        cfg = make_config(furnishing_strict=True)
        name, notes = rank(dict(PRIME_CONCIERGE, furnished="Furnished"), cfg)
        self.assertEqual(name, "Medium")
        self.assertIn("furnished - never High", notes)

    def test_without_strict_the_bonuses_still_win(self):
        cfg = make_config(furnishing_strict=False)
        name, _ = rank(dict(PRIME_CONCIERGE, furnished="Furnished"), cfg)
        self.assertEqual(name, "High")

    def test_part_furnished_is_not_capped(self):
        cfg = make_config(furnishing_strict=True)
        name, _ = rank(dict(PRIME_CONCIERGE, furnished="Part furnished"), cfg)
        self.assertEqual(name, "High")

    def test_unstated_furnishing_is_not_capped(self):
        cfg = make_config(furnishing_strict=True)
        name, _ = rank(dict(PRIME_CONCIERGE, furnished=None), cfg)
        self.assertEqual(name, "High")

    def test_landlord_flexible_is_not_capped(self):
        cfg = make_config(furnishing_strict=True)
        name, _ = rank(dict(PRIME_CONCIERGE, furnished="Furnished or unfurnished"), cfg)
        self.assertEqual(name, "High")

    def test_never_lowers_below_medium(self):
        cfg = make_config(furnishing_strict=True)
        self.assertEqual(core.apply_ceilings(2, {"furnished": "Furnished"}, cfg), (2, []))


class TestConciergeExpected(unittest.TestCase):
    def test_unmentioned_concierge_is_never_high(self):
        cfg = make_config(concierge="expected")
        name, notes = rank(dict(PRIME_CONCIERGE, concierge=None), cfg)
        self.assertEqual(name, "Medium")
        self.assertIn("no concierge mentioned - never High", notes)

    def test_stated_concierge_can_be_high(self):
        cfg = make_config(concierge="expected")
        self.assertEqual(rank(PRIME_CONCIERGE, cfg)[0], "High")

    def test_preferred_does_not_cap(self):
        cfg = make_config(concierge="preferred")
        self.assertEqual(rank(dict(PRIME_CONCIERGE, concierge=None, floor_number=None,
                                   lift=None, condition="refurbished"), cfg)[0], "High")

    def test_ignored_gives_no_bonus(self):
        cfg = make_config(concierge="ignored")
        tier, notes = core.apply_amenities(1, {"concierge": "yes"}, cfg)
        self.assertEqual(tier, 1)
        self.assertNotIn("concierge/porter", notes)


class TestSmallFloorplan(unittest.TestCase):
    """2026-09-28: the one High flat read ~600 sq ft off its floorplan."""

    def test_a_small_plan_size_is_never_high(self):
        cfg = make_config()
        name, notes = rank(dict(PRIME_CONCIERGE, size_sqft_plan=600), cfg)
        self.assertEqual(name, "Medium")
        self.assertIn("floorplan reads 600 sq ft (min 800) - never High", notes)

    def test_a_plan_size_at_the_minimum_is_not_capped(self):
        self.assertEqual(rank(dict(PRIME_CONCIERGE, size_sqft_plan=800), make_config())[0], "High")

    def test_a_stated_size_wins_over_the_plan(self):
        listing = dict(PRIME_CONCIERGE, size_sqft=950, size_sqft_plan=600)
        self.assertEqual(rank(listing, make_config())[0], "High")

    def test_no_minimum_means_no_cap(self):
        cfg = make_config(min_sqft=0)
        self.assertEqual(rank(dict(PRIME_CONCIERGE, size_sqft_plan=600), cfg)[0], "High")


class TestConfigValidation(unittest.TestCase):
    def test_unknown_concierge_mode_is_refused(self):
        import pathlib, tempfile
        from flatsearch import config
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "criteria.toml"
            p.write_text('[requirements]\nbudget_pcm = 3000\n'
                         '[preferences]\nconcierge = "required"\n', encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load(p)


if __name__ == "__main__":
    unittest.main()
