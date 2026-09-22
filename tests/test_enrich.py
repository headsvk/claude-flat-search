"""What the fetch layer adds to a cached page, and who is allowed to read it.

Three bugs, all found on 2026-09-22 from one listing that should never have been
shown (OpenRent 3048266, a lower ground floor flat recommended as High):

  * the floorplan read lived inline in the main fetch pass and was simply absent
    from the retry pass - and on Zoopla every listing but the first in a session
    is a stub, so every Zoopla listing goes through retry. 0 of 126 Zoopla pages
    ever carried a floorplan.
  * the plan was only read when the SIZE was unknown, so 230 live listings that
    state a size were never floor-checked at all.
  * the evidence line the fetch layer writes ("Floorplan area: 759 sq ft") was
    read back by the size scan as a size the PORTAL had stated, which put an
    OCR'd number in front of the hard filter.

The first two are tested through `enrich_detail` with a stub in place of the
network; the third is pure text.
"""
import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import fetch
from flatsearch import judge
from helpers import make_config


class FakePortal(dict):
    pass


ZOOPLA = FakePortal(name="Zoopla")


def run(coro):
    return asyncio.run(coro)


class TestEnrichGate(unittest.TestCase):
    """When is the floorplan worth reading at all?"""

    def setUp(self):
        self.calls = []
        self.plan = None
        self._real = fetch.floorplan_size

        async def fake(session, html, portal_name, cfg, url):
            self.calls.append(url)
            return self.plan

        fetch.floorplan_size = fake
        self.cfg = make_config()

    def tearDown(self):
        fetch.floorplan_size = self._real

    def enrich(self, text="", info=None, row=None):
        parts, info = [], dict(info or {})
        run(fetch.enrich_detail(None, ZOOPLA, "https://z/1", "<html></html>",
                                text, parts, info, dict(row or {}), self.cfg))
        return parts, info

    def test_size_and_floor_both_known_skips_the_plan(self):
        self.enrich(text="Set on the third floor.", row={"size_sqft": 900})
        self.assertEqual(self.calls, [])

    def test_size_known_but_floor_unknown_still_reads_the_plan(self):
        """The 230-listing gap: a stated size used to close the gate outright,
        so nothing ever checked whether the flat was lower ground."""
        self.enrich(text="A bright flat.", row={"size_sqft": 900})
        self.assertEqual(len(self.calls), 1)

    def test_floor_known_from_prose_is_enough(self):
        """judge.amenities is pure text, so the prose answer is free."""
        self.enrich(text="Situated on the fourth floor.", row={"size_sqft": 900})
        self.assertEqual(self.calls, [])

    def test_neither_known_reads_the_plan(self):
        self.enrich(text="A bright flat.")
        self.assertEqual(len(self.calls), 1)


class TestEnrichRecordsWhatThePlanSays(unittest.TestCase):
    def setUp(self):
        self._real = fetch.floorplan_size
        self.cfg = make_config()

    def tearDown(self):
        fetch.floorplan_size = self._real

    def enrich(self, plan, text="A bright flat.", row=None):
        async def fake(*a, **k):
            return plan
        fetch.floorplan_size = fake
        parts, info = [], {}
        run(fetch.enrich_detail(None, ZOOPLA, "https://z/1", "<html></html>",
                                text, parts, info, dict(row or {}), self.cfg))
        return parts, info

    def test_floor_only_plan_is_kept(self):
        """The Burnham Court case: dimensions but no total area. This used to
        return None and the floor went with it."""
        parts, info = self.enrich({"sqft": None, "floors": ["lower ground"],
                                   "image": "https://x/p.jpg"})
        self.assertEqual(info.get("floor_level"), "lower_ground")
        self.assertTrue(any("Floorplan floors" in p for p in parts))
        self.assertFalse(any("Floorplan area" in p for p in parts))

    def test_area_and_floor_both_recorded(self):
        parts, info = self.enrich({"sqft": 880, "basis": "gross", "confident": True,
                                   "evidence": "Approximate Area = 880 sq ft",
                                   "floors": ["second"], "image": "https://x/p.png"})
        self.assertEqual(info.get("size_sqft_plan"), 880)
        self.assertEqual(info.get("floor_number"), 2)
        self.assertTrue(any("Floorplan area: 880 sq ft" in p for p in parts))

    def test_a_stated_size_is_never_overwritten_by_the_plan(self):
        parts, info = self.enrich({"sqft": 700, "basis": "gross", "confident": True,
                                   "evidence": "700 sq ft", "floors": ["third"],
                                   "image": "https://x/p.png"},
                                  row={"size_sqft": 950})
        self.assertIsNone(info.get("size_sqft_plan"))
        self.assertEqual(info.get("floor_number"), 3)

    def test_nothing_found_adds_nothing(self):
        parts, info = self.enrich(None)
        self.assertEqual(parts, [])
        self.assertEqual(info, {})


class TestPlanAreaIsNotAStatedSize(unittest.TestCase):
    """fetch promises the plan size "cannot reach the hard filter". It could:
    the evidence line states a square footage in plain English, and the size
    scan read it straight back. 40 of 41 records holding a plan area held it
    as size_sqft."""

    CACHED = ("Description: A bright two bedroom flat with a private balcony.\n"
              "Floorplan area: 759 sq ft (total, clear) - read by OCR from https://x/y.jpg\n"
              "Floorplan text: Total area: approx. 70.5 sq. metres (758.6 sq. feet\n")

    def test_floorplan_lines_do_not_state_a_size(self):
        self.assertIsNone(judge.amenities(self.CACHED).get("size_sqft"))

    def test_a_portal_stated_size_still_reads(self):
        self.assertEqual(
            judge.amenities(self.CACHED + "Size: 1,104 sq ft\n").get("size_sqft"), 1104)

    def test_floor_off_the_plan_still_reads(self):
        """Only the SIZE is refused from those lines - the floor is the whole
        point of writing them."""
        text = self.CACHED + "Floorplan floors: lower ground - read by OCR from https://x/y.jpg\n"
        self.assertEqual(judge.amenities(text).get("floor_level"), "lower_ground")


if __name__ == "__main__":
    unittest.main()
