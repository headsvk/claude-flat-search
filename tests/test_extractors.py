"""Extractor and filter tests.

Every case here is a real failure this project has actually shipped. The
characteristic bug is that a broken extractor does not crash — it quietly
produces a full tracker in which every listing says "no A/C mentioned", or
silently drops a fifth of one portal's fetches. Nothing but a test catches that,
because the run looks completely healthy either way.

    python -m unittest discover -s tests -v
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import core as hunt
from flatsearch import judge
from flatsearch import portals
from helpers import make_config


# ---------------------------------------------------------------------------
# The end-anchor bug: cost 22% of OpenRent fetches, with no error at all.
# ---------------------------------------------------------------------------

class TestFindHeading(unittest.TestCase):
    """An end anchor names a HEADING, so it may only match at the start of a
    line. Matched as a bare substring it fires inside the prose it is meant to
    terminate."""

    def test_matches_at_start_of_line(self):
        text = "description\nsome prose here\nlocation\nmore".lower()
        self.assertEqual(portals._find_heading(text, "location", 0),
                         text.index("location\n"))

    def test_does_not_match_mid_line(self):
        """OpenRent's stock copy: 'a flat in a great location'. Matching that
        truncated descriptions below minlen and dropped the listing entirely."""
        text = "this flat is in a great location for commuters".lower()
        self.assertEqual(portals._find_heading(text, "location", 0), -1)

    def test_skips_mid_line_hit_and_finds_the_real_heading(self):
        text = ("a flat in a great location for commuters\n"
                "location\nHackney").lower()
        got = portals._find_heading(text, "location", 0)
        self.assertEqual(text[got:got + 8], "location")
        self.assertEqual(text[got - 1], "\n")

    def test_match_at_offset_zero_counts(self):
        self.assertEqual(portals._find_heading("location\nfoo", "location", 0), 0)


class TestSliceBetween(unittest.TestCase):
    def test_openrent_boilerplate_does_not_truncate(self):
        """The regression that lost 22% of OpenRent: the description must
        survive the word 'location' appearing inside it."""
        text = ("Description\n"
                "A wonderful two bedroom apartment set in a great location for "
                "commuters, with two bathrooms, a private balcony and access to "
                "a residents gym. Available immediately on a long let.\n"
                "Location\n"
                "Hackney Central")
        got = portals.slice_between(text, ["Description"], ["Location"])
        self.assertIn("residents gym", got)
        self.assertNotIn("Hackney Central", got)

    def test_returns_empty_below_minlen(self):
        self.assertEqual(portals.slice_between("Description\nTiny.", ["Description"],
                                               ["Location"]), "")

    def test_missing_start_anchor_returns_empty(self):
        self.assertEqual(portals.slice_between("nothing here", ["Description"],
                                               ["Location"]), "")


# ---------------------------------------------------------------------------
# Price. Rightmove quotes prime listings per WEEK.
# ---------------------------------------------------------------------------

class TestPrice(unittest.TestCase):
    def test_pcm_direct(self):
        self.assertEqual(portals.pcm_from_text("£3,250 pcm"), 3250)

    def test_weekly_is_converted(self):
        """£1,000 pw is £4,333 pcm - it is not a £1,000 flat, and reading it as
        one puts a £4,333 listing under a £3,000 budget."""
        self.assertEqual(portals.pcm_from_text("£1,000 pw"), 4333)

    def test_per_week_spelled_out(self):
        self.assertEqual(portals.pcm_from_text("£750 per week"), 3250)

    def test_openrent_separator_form(self):
        """OpenRent renders '£3,250 | per month'. Without tolerating the
        separator, measured OpenRent price coverage was 1% - and 817 listings
        with no price all pass a budget filter vacuously."""
        for text in ("£3,250 | per month", "£3,250 · per month", "£3,250 - per month"):
            with self.subTest(text=text):
                self.assertEqual(portals.pcm_from_text(text), 3250)

    def test_monthly_preferred_over_weekly_when_both_present(self):
        self.assertEqual(portals.pcm_from_text("£3,000 pcm (£692 pw)"), 3000)

    def test_no_price_is_none_not_zero(self):
        """None means unknown. Zero would silently pass every budget filter."""
        self.assertIsNone(portals.pcm_from_text("Price on application"))

    def test_money_first_amount(self):
        self.assertEqual(portals.money("from £1,250,000"), 1250000)
        self.assertIsNone(portals.money(None))


# ---------------------------------------------------------------------------
# Size. MIN_SQFT is a hard filter, so a wrong size DELETES a listing.
# ---------------------------------------------------------------------------

class TestSqft(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(portals.sqft("966 sq ft"), 966)

    def test_variants(self):
        for text in ("1,200 sq.ft", "1,200 sqft", "1,200 square feet".replace("square", "sq")):
            with self.subTest(text=text):
                self.assertEqual(portals.sqft(text), 1200)

    def test_ignores_a_balcony(self):
        """'86 sq ft' in a card is a terrace, not the flat."""
        self.assertIsNone(portals.sqft("balcony of 86 sq ft"))

    def test_takes_the_largest_plausible(self):
        self.assertEqual(portals.sqft("1,010 sq ft flat with a 90 sq ft balcony"), 1010)

    def test_rejects_implausible(self):
        self.assertIsNone(portals.sqft("99999 sq ft"))

    def test_none_input(self):
        self.assertIsNone(portals.sqft(None))


class TestSizeFromJson(unittest.TestCase):
    def test_reads_plain_json(self):
        html = '{"sizeSqFeet":"1024","other":1}'
        self.assertEqual(portals.size_from_json(html, portals.ZOOPLA_SIZE_RE), 1024)

    def test_reads_backslash_escaped_json(self):
        """Some of this JSON sits inside a JS string literal."""
        html = '\\"minimumAreaSqFt\\":947'
        self.assertEqual(portals.size_from_json(html, portals.OTM_SIZE_RE), 947)

    def test_empty_string_value_does_not_match(self):
        """Zoopla writes "" when it does not know, which must fall through to
        the nested floorArea fallback rather than returning 0."""
        html = '{"sizeSqFeet":""}'
        self.assertIsNone(portals.size_from_json(html, portals.ZOOPLA_SIZE_RE))

    def test_rejects_implausible_value(self):
        self.assertIsNone(portals.size_from_json('{"sizeSqFeet":"12"}',
                                                 portals.ZOOPLA_SIZE_RE))

    def test_no_match_is_none(self):
        self.assertIsNone(portals.size_from_json("<html></html>",
                                                 portals.ZOOPLA_SIZE_RE))


class TestUntag(unittest.TestCase):
    def test_strips_tags_and_unescapes(self):
        self.assertEqual(portals.untag("<p>Two&nbsp;beds<br>Two baths</p>"),
                         "Two beds Two baths")


# ---------------------------------------------------------------------------
# A/C pre-filter. It may only ever downgrade work to a model, never upgrade:
# it cannot emit `yes` on its own.
# ---------------------------------------------------------------------------

class TestScan(unittest.TestCase):
    def test_finds_terms(self):
        hits, _ = judge.scan("Full air conditioning throughout")
        self.assertIn("air condition", hits)

    def test_comfort_cooling_counts(self):
        """The standard prime-London term for the same thing."""
        hits, _ = judge.scan("Benefits from comfort cooling")
        self.assertIn("comfort cooling", hits)

    def test_silent_listing_has_no_hits(self):
        hits, _ = judge.scan("A bright two bedroom flat with a balcony.")
        self.assertEqual(hits, [])

    def test_decoys_are_flagged_not_counted_as_aircon(self):
        hits, decoys = judge.scan("Underfloor heating and a ceiling fan")
        self.assertEqual(hits, [])
        self.assertIn("ceiling fan", decoys)
        self.assertIn("underfloor heating", decoys)


class TestWindows(unittest.TestCase):
    """The review payload carries windows round the matched terms rather than
    the whole page. Every window must be a contiguous verbatim slice: `commit`
    re-validates the model's quote against the full cached text, so a quote
    spanning a gap would silently collapse to `unstated`."""

    def test_window_is_a_verbatim_slice(self):
        text = "x" * 600 + " full air conditioning throughout " + "y" * 600
        got = judge.windows(text, ["air condition"])
        for piece in got.split(judge.GAP):
            self.assertIn(piece, text)

    def test_keeps_the_matched_phrase_and_its_context(self):
        text = "x" * 600 + " full air conditioning throughout " + "y" * 600
        got = judge.windows(text, ["air condition"])
        self.assertIn("full air conditioning throughout", got)
        self.assertLess(len(got), len(text))

    def test_overlapping_windows_merge_without_a_gap_marker(self):
        text = "air conditioning is here and comfort cooling is also here"
        got = judge.windows(text, ["air condition", "comfort cooling"])
        self.assertNotIn(judge.GAP, got)
        self.assertEqual(got, text)

    def test_distant_matches_are_separated_by_the_gap_marker(self):
        text = ("air conditioning in the flat" + "z" * 2000 +
                "comfort cooling in the gym")
        got = judge.windows(text, ["air condition", "comfort cooling"])
        self.assertIn(judge.GAP, got)
        self.assertIn("air conditioning in the flat", got)
        self.assertIn("comfort cooling in the gym", got)

    def test_short_text_is_returned_whole(self):
        text = "air conditioning throughout"
        self.assertEqual(judge.windows(text, ["air condition"]), text)

    def test_no_hits_returns_full_text(self):
        self.assertEqual(judge.windows("nothing relevant", []), "nothing relevant")


# ---------------------------------------------------------------------------
# Quote validation. This is what makes a model's A/C claim trustworthy: an
# unbacked claim is worth less than no claim, because it looks like evidence.
# ---------------------------------------------------------------------------

class TestValidateVerdict(unittest.TestCase):
    URL = "https://www.example.com/properties/1"
    PAGE = "A superb flat with full comfort cooling to all rooms."

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = pathlib.Path(self.tmp.name)
        hunt.write_json(self.cache / hunt.cache_name(self.URL),
                        {"url": self.URL, "text": self.PAGE, "fetched_at": "2026-09-19"})
        self.addCleanup(self.tmp.cleanup)

    def test_backed_claim_survives(self):
        out = hunt.validate_verdict(self.URL, {
            "verdict": "yes", "scope": "in_unit",
            "evidence": "full comfort cooling to all rooms"}, self.cache)
        self.assertEqual(out["verdict"], "yes")
        self.assertEqual(out["scope"], "in_unit")
        self.assertEqual(out["flags"], [])

    def test_quote_not_on_the_page_collapses_to_unstated(self):
        out = hunt.validate_verdict(self.URL, {
            "verdict": "yes", "scope": "in_unit",
            "evidence": "air conditioning in every bedroom"}, self.cache)
        self.assertEqual(out["verdict"], "unstated")
        self.assertEqual(out["evidence"], "")
        self.assertTrue(out["flags"])

    def test_yes_with_no_quote_collapses_to_unstated(self):
        out = hunt.validate_verdict(self.URL, {
            "verdict": "yes", "scope": "in_unit", "evidence": ""}, self.cache)
        self.assertEqual(out["verdict"], "unstated")

    def test_whitespace_differences_are_tolerated(self):
        """A model re-wrapping a quote is not a fabricated quote."""
        out = hunt.validate_verdict(self.URL, {
            "verdict": "yes", "scope": "in_unit",
            "evidence": "full   comfort\n cooling  to all rooms"}, self.cache)
        self.assertEqual(out["verdict"], "yes")

    def test_unknown_verdict_word_collapses_to_unstated(self):
        out = hunt.validate_verdict(self.URL, {
            "verdict": "probably", "scope": "", "evidence": "x"}, self.cache)
        self.assertEqual(out["verdict"], "unstated")

    def test_no_verdict_at_all_is_unchecked_not_unstated(self):
        """Never judged and judged-as-silent are different facts."""
        self.assertEqual(hunt.validate_verdict(self.URL, None, self.cache)["verdict"],
                         "unchecked")

    def test_missing_cache_file_discards_the_verdict(self):
        out = hunt.validate_verdict("https://www.example.com/properties/999", {
            "verdict": "yes", "scope": "in_unit", "evidence": "x"}, self.cache)
        self.assertEqual(out["verdict"], "unchecked")
        self.assertTrue(out["flags"])


# ---------------------------------------------------------------------------
# District parsing and tiering.
# ---------------------------------------------------------------------------

CFG = make_config()


class TestDistrict(unittest.TestCase):
    def test_reads_from_postcode(self):
        self.assertEqual(hunt.district({"postcode": "NW8 9AB"}), "NW8")

    def test_falls_back_to_area(self):
        self.assertEqual(hunt.district({"area": "Westferry Circus, E14"}), "E14")

    def test_unparseable_address_is_none(self):
        self.assertIsNone(hunt.district({"area": "Richmond, Surrey"}))

    def test_full_outward_code_stems_to_its_district(self):
        """'Sloane Street, London, SW1X' must reach the prime tier. Without the
        stem fallback, SW1X / W1K / WC2R rank as unknown."""
        self.assertEqual(CFG.tier_of("SW1X"), 0)

    def test_tiers(self):
        self.assertEqual(CFG.tier_of("NW8"), 0)
        self.assertEqual(CFG.tier_of("SW6"), 1)
        self.assertEqual(CFG.tier_of("E14"), 2)

    def test_unlisted_district_has_no_tier(self):
        self.assertIsNone(CFG.tier_of("BR1"))

    def test_none_district_has_no_tier(self):
        self.assertIsNone(CFG.tier_of(None))


# ---------------------------------------------------------------------------
# UNKNOWN NEVER REJECTS. This has broken and been re-fixed three times.
# ---------------------------------------------------------------------------

class TestHardFilterRejects(unittest.TestCase):
    def base(self, **kw):
        row = {"type": "unit", "postcode": "NW8 9AB", "price_pcm": 2500,
               "bed_count": 2, "bathrooms": 2}
        row.update(kw)
        return row

    def test_clean_listing_survives(self):
        self.assertIsNone(hunt.hard_filter(self.base(), CFG))

    def test_over_budget(self):
        self.assertIn("over budget", hunt.hard_filter(self.base(price_pcm=3500), CFG))

    def test_too_few_bedrooms(self):
        self.assertIn("bed", hunt.hard_filter(self.base(bed_count=1), CFG))

    def test_too_few_bathrooms(self):
        self.assertIn("bath", hunt.hard_filter(self.base(bathrooms=1), CFG))

    def test_stated_size_below_floor(self):
        self.assertIn("sq ft", hunt.hard_filter(self.base(size_sqft=600), CFG))

    def test_basement_is_an_outright_no(self):
        for level in ("basement", "lower_ground"):
            with self.subTest(level=level):
                self.assertIn("lower ground",
                              hunt.hard_filter(self.base(floor_level=level), CFG))

    def test_district_outside_the_lists(self):
        self.assertIn("not in affluent list",
                      hunt.hard_filter(self.base(postcode="BR1 1AA"), CFG))


class TestHardFilterUnknowns(unittest.TestCase):
    """An unstated size, district, bathroom count, lift or A/C is recorded and
    flagged - never treated as absence. Each of these has regressed before."""

    def base(self, **kw):
        row = {"type": "unit", "postcode": "NW8 9AB", "price_pcm": 2500,
               "bed_count": 2, "bathrooms": 2}
        row.update(kw)
        return row

    def test_unknown_price_survives(self):
        self.assertIsNone(hunt.hard_filter(self.base(price_pcm=None), CFG))

    def test_unknown_size_survives(self):
        """~60% of listings state no size. Requiring one deletes the market."""
        self.assertIsNone(hunt.hard_filter(self.base(size_sqft=None), CFG))

    def test_unknown_bathrooms_survives(self):
        """OpenRent never prints a bathroom count on the card."""
        self.assertIsNone(hunt.hard_filter(self.base(bathrooms=None), CFG))

    def test_unknown_bedrooms_survives(self):
        self.assertIsNone(hunt.hard_filter(self.base(bed_count=None), CFG))

    def test_unparseable_district_survives(self):
        """'Richmond, Surrey' is an UNKNOWN district, not a bad one - measured
        at 4 of 9 good Richmond hits."""
        row = self.base()
        row.pop("postcode")
        row["area"] = "Richmond, Surrey"
        self.assertIsNone(hunt.hard_filter(row, CFG))

    def test_area_trusted_bypasses_the_district_check(self):
        row = self.base(postcode="BR1 1AA", area_trusted=True)
        self.assertIsNone(hunt.hard_filter(row, CFG))

    def test_districts_only_false_never_rejects_on_district(self):
        cfg = make_config(districts_only=False)
        self.assertIsNone(hunt.hard_filter(self.base(postcode="BR1 1AA"), cfg))


class TestConfigHelpers(unittest.TestCase):
    def test_districts_are_normalised_for_matching(self):
        self.assertEqual(CFG.prime_districts, ("w1", "sw1", "sw3", "nw8"))

    def test_values_are_real_types_not_strings(self):
        """The whole point of moving off markdown: no cfg_int/cfg_list needed."""
        self.assertIsInstance(CFG.budget_pcm, int)
        self.assertIsInstance(CFG.districts_only, bool)
        self.assertIsInstance(CFG.prime_districts, tuple)

    def test_cache_name_is_stable_and_filesystem_safe(self):
        a = hunt.cache_name("https://www.example.com/properties/1")
        self.assertEqual(a, hunt.cache_name("https://www.example.com/properties/1"))
        self.assertNotIn("/", a)


if __name__ == "__main__":
    unittest.main()
