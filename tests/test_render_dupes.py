"""One flat, several ads - and one building, several flats.

Measured on the live set 2026-09-20: 62 groups covering 135 listings, 73 rows
of them redundant, 36 spanning more than one portal. Ensign House appeared
twice in that morning's file at the same price and size, once from Rightmove
and once from Zoopla; Ostro Tower had six rows and Circus Apartments seven.
Nothing in the output said so, so the headline count could only be trusted
after reading the table by eye.

The two cases are deliberately handled differently. Several ads for one flat
are noise and collapse. Several flats in one building are real, separate
options and must not collapse - they are annotated instead. And collapsing a
row must never collapse the STAMP: every ad still has to be marked reported,
or the hidden ones come back as new tomorrow, for ever.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import render  # noqa: E402


def listing(url, **kw):
    row = {"url": url, "title": "2 bed flat", "platform": "Rightmove",
           "price_pcm": 4500, "bedrooms": 2, "bathrooms": 2,
           "priority": "High", "status": "NEW", "area": "Ensign House, SW18",
           "postcode": "Ensign House, SW18", "aircon": "unstated",
           "aircon_checked": "2026-09-20",
           "found_on": "2026-09-20", "reported_on": None}
    row.update(kw)
    return row


def daily(rows, day="2026-09-20"):
    text, fresh, held, shown = render.render_daily({"listings": rows}, {}, day)
    return text, fresh


def body_rows(text):
    return [l for l in text.splitlines()
            if l.startswith("|") and not l.startswith("|---") and "£pcm" not in l]


class TestDuplicateAds(unittest.TestCase):

    def test_the_same_flat_on_two_portals_is_one_row(self):
        text, _ = daily([listing("https://rightmove/1"),
                         listing("https://zoopla/1", platform="Zoopla")])
        self.assertEqual(len(body_rows(text)), 1)

    def test_the_collapsed_row_still_carries_every_link(self):
        """Collapsing must not hide a listing - the other ad may be the one
        that answers the phone."""
        text, _ = daily([listing("https://rightmove/1"),
                         listing("https://zoopla/1", platform="Zoopla")])
        self.assertIn("https://rightmove/1", text)
        self.assertIn("https://zoopla/1", text)
        self.assertIn("[Zoopla](https://zoopla/1)", text)

    def test_every_ad_is_still_stamped_reported(self):
        """The trap. A row that is not shown is still SPENT: if only the
        representative is stamped, the hidden ad has no reported_on and comes
        back as new tomorrow morning, and the morning after that."""
        _, fresh = daily([listing("https://rightmove/1"),
                          listing("https://zoopla/1", platform="Zoopla")])
        self.assertEqual({l["url"] for l in fresh},
                         {"https://rightmove/1", "https://zoopla/1"})

    def test_a_different_price_is_a_different_flat(self):
        text, _ = daily([listing("https://rightmove/1"),
                         listing("https://zoopla/1", platform="Zoopla",
                                 price_pcm=3900)])
        self.assertEqual(len(body_rows(text)), 2)

    def test_two_stated_floors_that_disagree_are_two_flats(self):
        """Measured 2026-09-20: four ads at GBP 3,700 in The Canopy, same
        address, same bed/bath count, sitting on two different floors. Price
        alone called them one flat - a claim the data contradicts."""
        text, _ = daily([listing("https://a/1", floor=1),
                         listing("https://a/2", floor=2)])
        self.assertEqual(len(body_rows(text)), 2)

    def test_a_blank_field_does_not_block_the_merge(self):
        """Most listings state neither floor nor size. Requiring agreement on
        an unknown would merge nothing at all."""
        text, _ = daily([listing("https://a/1", floor=1),
                         listing("https://a/2", floor=None, platform="Zoopla")])
        self.assertEqual(len(body_rows(text)), 1)

    def test_two_stated_sizes_that_disagree_are_two_flats(self):
        text, _ = daily([listing("https://a/1", size_sqft=900),
                         listing("https://a/2", size_sqft=1100)])
        self.assertEqual(len(body_rows(text)), 2)

    def test_the_note_does_not_claim_more_than_is_known(self):
        """The ads agree on everything they state. That is not the same as
        being one flat, and the row must not say it is."""
        text, _ = daily([listing("https://a/1"),
                         listing("https://a/2", platform="Zoopla")])
        self.assertIn("2 near-identical ads", text)

    def test_a_listing_with_no_price_never_merges(self):
        """Unknown never merges, for the same reason unknown never rejects:
        two blanks are not evidence of being the same flat."""
        text, _ = daily([listing("https://a/1", price_pcm=None),
                         listing("https://a/2", price_pcm=None)])
        self.assertEqual(len(body_rows(text)), 2)

    def test_the_headline_says_how_many_distinct_flats(self):
        text, _ = daily([listing("https://rightmove/1"),
                         listing("https://zoopla/1", platform="Zoopla")])
        self.assertIn("**2 new** (1 distinct flats)", text)


class TestBuildingClusters(unittest.TestCase):

    def test_different_flats_in_one_building_are_not_collapsed(self):
        rows = [listing("https://a/%d" % i, price_pcm=3000 + 100 * i)
                for i in range(4)]
        text, _ = daily(rows)
        self.assertEqual(len(body_rows(text)), 4)

    def test_they_are_annotated_with_the_building_count(self):
        rows = [listing("https://a/%d" % i, price_pcm=3000 + 100 * i)
                for i in range(4)]
        text, _ = daily(rows)
        self.assertIn("4 units in this building", text)

    def test_a_lone_flat_gets_no_building_note(self):
        text, _ = daily([listing("https://a/1")])
        self.assertNotIn("units in this building", text)

    def test_the_count_spans_the_whole_tracker_not_just_today(self):
        """Six units going in one block is a fact about the block, whether or
        not five of them turned up last week."""
        today = listing("https://a/new")
        earlier = [listing("https://a/%d" % i, price_pcm=3000 + 100 * i,
                           reported_on="2026-09-18") for i in range(5)]
        text, _ = daily([today] + earlier)
        self.assertIn("6 units in this building", text)

    def test_a_ruled_out_listing_does_not_inflate_the_count(self):
        today = listing("https://a/new")
        dead = listing("https://a/dead", price_pcm=3100, status="REJECTED",
                       reported_on="2026-09-18")
        text, _ = daily([today, dead])
        self.assertNotIn("units in this building", text)


if __name__ == "__main__":
    unittest.main()
