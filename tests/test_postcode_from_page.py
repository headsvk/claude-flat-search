"""Rightmove's address line often omits the postcode its page title states.

2026-09-28: "4-7 Lombard Lane, London" on the card, "...London, EC4Y" in the
title. An unknown district is never rejected, so the flat ranked High against
an area list that did not contain EC4.
"""
import unittest

from helpers import make_config  # noqa: F401  (puts src on the path)
from flatsearch import fetch, portals

TITLE = ("<html><head><title>2 bedroom apartment for rent in 4-7 Lombard Lane, "
         "London, EC4Y</title></head><body></body></html>")


class TestRightmoveTitle(unittest.TestCase):
    def test_the_title_postcode_is_read(self):
        _, info = portals.rightmove_detail(TITLE)
        self.assertEqual(info["postcode"], "4-7 Lombard Lane, London, EC4Y")

    def test_a_full_postcode_is_kept_whole(self):
        html = TITLE.replace("EC4Y", "EC4Y 8AD")
        self.assertEqual(portals.rightmove_detail(html)[1]["postcode"],
                         "4-7 Lombard Lane, London, EC4Y 8AD")

    def test_a_title_without_a_postcode_states_nothing(self):
        html = TITLE.replace(", EC4Y", "")
        self.assertNotIn("postcode", portals.rightmove_detail(html)[1])

    def test_a_later_svg_title_is_not_read(self):
        html = ("<title>Flat for rent in Kings Road, London</title>"
                "<svg><title>log_in</title></svg>")
        self.assertNotIn("postcode", portals.rightmove_detail(html)[1])


class TestMergeDetail(unittest.TestCase):
    def test_a_stated_postcode_replaces_an_address_without_one(self):
        row = {"postcode": "4-7 Lombard Lane, London"}
        fetch.merge_detail(row, {"postcode": "4-7 Lombard Lane, London, EC4Y"})
        self.assertEqual(row["postcode"], "4-7 Lombard Lane, London, EC4Y")

    def test_an_address_with_a_postcode_is_not_overwritten(self):
        row = {"postcode": "Water Lane NW1"}
        fetch.merge_detail(row, {"postcode": "Water Lane, London, NW5"})
        self.assertEqual(row["postcode"], "Water Lane NW1")

    def test_other_fields_still_only_fill_blanks(self):
        row = {"furnished": "Furnished", "lift": None}
        fetch.merge_detail(row, {"furnished": "Unfurnished", "lift": "yes"})
        self.assertEqual(row, {"furnished": "Furnished", "lift": "yes"})


if __name__ == "__main__":
    unittest.main()
