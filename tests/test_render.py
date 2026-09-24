"""Daily-file selection tests.

The rule that makes the daily file work is that a listing appears in exactly ONE
of them, ever. That rule is only safe if a listing is reported when it is
COMPLETE, because its single appearance cannot be spent twice.

STAGE2_CAP breaks the pairing: a busy morning commits far more listings than it
fetches details for. Measured 2026-09-19, 142 of 179 went into the daily file as
stubs - no size, floor, lift or A/C - and were stamped, so the filled-in versions
could never be shown. These tests pin down the fix and its escape hatch.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import render


def listing(url="https://www.example.com/1", **kw):
    row = {"url": url, "title": "2 bed flat", "platform": "Rightmove",
           "price_pcm": 3000, "bedrooms": 2, "bathrooms": 2,
           "priority": "High", "status": "NEW", "area": "NW8",
           "found_on": "2026-09-19", "reported_on": None}
    row.update(kw)
    return row


def daily(rows, day="2026-09-19"):
    text, fresh, held, shown = render.render_daily({"listings": rows}, {}, day)
    return {"text": text, "fresh": fresh, "held": held, "shown": shown}


class TestHoldBackUnfetched(unittest.TestCase):

    def test_a_fetched_listing_is_reported(self):
        got = daily([listing(aircon_checked="2026-09-19", aircon="unstated")])
        self.assertEqual(got["shown"], 1)
        self.assertEqual(len(got["held"]), 0)

    def test_an_unfetched_listing_is_held_not_reported(self):
        """Its detail page has not been read, so every column that matters is
        blank. Reporting it now spends its one appearance on a stub."""
        got = daily([listing(aircon="unchecked")])
        self.assertEqual(got["shown"], 0)
        self.assertEqual(len(got["held"]), 1)

    def test_a_held_listing_is_not_stamped(self):
        """The stamp is what makes the loss permanent."""
        got = daily([listing(aircon="unchecked")])
        self.assertEqual(got["fresh"], [])

    def test_a_held_listing_is_reported_once_its_details_arrive(self):
        row = listing(aircon="unchecked")
        self.assertEqual(daily([row])["shown"], 0)
        row["aircon_checked"] = "2026-09-20"
        row["aircon"] = "unstated"
        row["size_sqft"] = 950
        later = daily([row], day="2026-09-20")
        self.assertEqual(later["shown"], 1)
        self.assertEqual(len(later["fresh"]), 1)

    def test_the_file_says_how_many_are_waiting(self):
        """Held listings must be visible as a number. Silently withholding them
        would be the same class of bug in the other direction."""
        rows = [listing("https://www.example.com/%d" % i, aircon="unchecked")
                for i in range(5)]
        rows.append(listing("https://www.example.com/x", aircon_checked="2026-09-19"))
        got = daily(rows)
        self.assertIn("5 more", got["text"])

    def test_waiting_count_shown_even_when_nothing_is_reportable(self):
        got = daily([listing(aircon="unchecked")])
        self.assertIn("waiting on a detail page", got["text"])


class TestOverdueEscapeHatch(unittest.TestCase):
    """A listing whose detail fetch keeps failing must not be held for ever -
    that would be the very silent loss the hold exists to prevent."""

    def test_held_before_the_deadline(self):
        row = listing(aircon="unchecked", found_on="2026-09-19")
        self.assertEqual(daily([row], day="2026-09-25")["shown"], 0)

    def test_released_after_the_deadline(self):
        row = listing(aircon="unchecked", found_on="2026-09-19")
        got = daily([row], day="2026-09-26")
        self.assertEqual(got["shown"], 1)

    def test_deadline_boundary_is_hold_days(self):
        row = listing(aircon="unchecked", found_on="2026-09-19")
        self.assertTrue(render.overdue(row, "2026-09-26"))
        self.assertFalse(render.overdue(row, "2026-09-25"))

    def test_unparseable_dates_do_not_release_early(self):
        self.assertFalse(render.overdue(listing(found_on=None), "2026-09-19"))
        self.assertFalse(render.overdue(listing(found_on="whenever"), "2026-09-19"))


class TestRejectedAreUnaffected(unittest.TestCase):
    """A rejected listing never gets an A/C verdict - commit skips validation for
    anything the hard filter caught - so it must not be held for one."""

    def test_rejected_listing_is_still_reported(self):
        got = daily([listing(status="REJECTED", aircon="unchecked",
                             notes="rejected on detail: 615 sq ft (min 800)")])
        self.assertEqual(got["shown"], 1)
        self.assertEqual(len(got["held"]), 0)

    def test_rejected_listing_appears_in_the_ruled_out_section(self):
        got = daily([listing(status="REJECTED", aircon="unchecked",
                             notes="rejected on detail: 615 sq ft (min 800)")])
        self.assertIn("Ruled out on sight", got["text"])
        self.assertIn("615 sq ft", got["text"])


class TestRerunSameDay(unittest.TestCase):
    """Generating twice in one morning must reproduce the file, not blank it."""

    def test_already_stamped_today_still_appears(self):
        row = listing(aircon_checked="2026-09-19", reported_on="2026-09-19")
        got = daily([row])
        self.assertEqual(got["shown"], 1)
        self.assertEqual(got["fresh"], [])

    def test_a_listing_from_an_earlier_day_does_not_reappear(self):
        row = listing(aircon_checked="2026-09-17", reported_on="2026-09-17")
        self.assertEqual(daily([row])["shown"], 0)


class TestAirconScope(unittest.TestCase):
    """Only cooling in the flat itself is A/C. The residents' gym is not."""

    def rows(self):
        return [listing(url="https://www.example.com/unit", aircon="yes",
                        aircon_scope="in_unit", aircon_checked="2026-09-25"),
                listing(url="https://www.example.com/gym", aircon="yes",
                        aircon_scope="communal_only", aircon_checked="2026-09-25",
                        area="E1", price_pcm=3100)]

    def test_the_headline_counts_only_in_unit(self):
        text = daily(self.rows(), day="2026-09-25")["text"]
        self.assertIn("1 with A/C in unit", text)

    def test_communal_cooling_is_not_bolded_as_ac(self):
        self.assertEqual(render.flags(self.rows()[0]).count("**A/C**"), 1)
        flag = render.flags(self.rows()[1])
        self.assertNotIn("**A/C**", flag)
        self.assertIn("A/C communal only", flag)


if __name__ == "__main__":
    unittest.main()
