"""Server-side search windows: `added=` and `available_from=`.

Both narrow a search at the portal instead of fetching and discarding, and both
have a direction that loses listings silently if it is got backwards - which is
the only reason these tests exist:

  * a RECENCY window narrower than the gap since the last run never fetches
    what arrived in between, and nothing downstream can tell a listing filtered
    out at the portal from one that was never posted;
  * an AVAILABILITY window wider than the move-in date admits listings that are
    too late while claiming they were verified.

The parameter VALUES are not asserted against a portal here - they are measured
against the live result counter and written down in portals.py. What is asserted
is the arithmetic that picks between them.
"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import core, fetch, portals  # noqa: E402
from helpers import make_config  # noqa: E402

ZOOPLA = ("https://www.zoopla.co.uk/to-rent/property/london/?beds_min=2"
          "&baths_min=2&price_max=4500&results_sort=newest_listings")
RIGHTMOVE = ("https://www.rightmove.co.uk/property-to-rent/find.html?"
             "minBedrooms=2&maxPrice=4500&sortType=6")

OTM = "https://www.onthemarket.com/to-rent/property/london/?max-price=4500"

# OpenRent: an availability date, but no recency filter found.
OPENRENT = "https://www.openrent.co.uk/properties-to-rent/london?prices_max=4500"



def cfg_with_state(tmp, days_ago=None, **over):
    """A config whose state.json was last written `days_ago` days ago.

    None means no state file at all, which is a fresh clone.
    """
    cfg = make_config(tmp, **over)
    if days_ago is not None:
        cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now() - dt.timedelta(days=days_ago)
        cfg.state_path.write_text(json.dumps(
            {"updated": stamp.isoformat(timespec="seconds"), "listings": []}),
            encoding="utf-8")
    return cfg


class TestSnap(unittest.TestCase):
    OFFERED = {1: "a", 3: "b", 7: "c", 14: "d", 30: "e"}

    def test_up_takes_the_smallest_window_that_covers(self):
        self.assertEqual(portals.snap_up(4, self.OFFERED), 7)

    def test_up_takes_an_exact_size_unchanged(self):
        self.assertEqual(portals.snap_up(7, self.OFFERED), 7)

    def test_up_refuses_rather_than_narrowing(self):
        """Past the widest window there is no safe answer, so there is none.

        Returning 30 here would be the silent-loss bug this whole module is
        written to avoid: a 60-day absence covered by a 30-day window loses a
        month of listings with nothing to show it happened.
        """
        self.assertIsNone(portals.snap_up(60, self.OFFERED))


class TestRecencyWindow(unittest.TestCase):
    """How far back a run asks the portal to look."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def scope(self, **kw):
        cfg = cfg_with_state(self.tmp, searches=(ZOOPLA,), **kw)
        return fetch.scope_search_url(ZOOPLA, cfg, fetch.days_since_last_run(cfg))

    def test_a_first_run_uses_the_configured_reach(self):
        url, note = self.scope(days_ago=None)
        self.assertIn("added=14_days", url)
        self.assertIn("first run", note)

    def test_a_daily_run_asks_for_three_days(self):
        """The margin is what makes a same-day gap ask for 3 days rather than 1:
        listings keep arriving while the previous run is still fetching."""
        url, _ = self.scope(days_ago=0)
        self.assertIn("added=3_days", url)

    def test_a_skipped_morning_widens_the_window(self):
        url, _ = self.scope(days_ago=5)
        self.assertIn("added=7_days", url)

    def test_a_long_absence_is_not_scoped_at_all(self):
        """The window would have to be narrower than the gap, so there is none.
        Paging the backlog is the cost of having been away."""
        url, note = self.scope(days_ago=45)
        self.assertNotIn("added=", url)
        self.assertIn("not scoped", note)

    def test_zero_turns_it_off(self):
        url, note = self.scope(days_ago=0, first_run_days=0)
        self.assertNotIn("added=", url)
        self.assertIsNone(note)

    def test_a_portal_with_no_measured_window_is_left_alone(self):
        """OpenRent exposes no date-added filter, and it is the portal that
        would benefit most - it is unsorted, so --incremental cannot stop early
        there either. It walks its whole result set, as it always has."""
        cfg = cfg_with_state(self.tmp, days_ago=0, searches=(OPENRENT,))
        url, note = fetch.scope_search_url(OPENRENT, cfg, 0)
        self.assertEqual(url, OPENRENT)
        self.assertIsNone(note)

    def test_onthemarket_uses_its_own_spelling(self):
        cfg = cfg_with_state(self.tmp, days_ago=0, searches=(OTM,))
        url, _ = fetch.scope_search_url(OTM, cfg, 0)
        self.assertIn("recently-added=3-days", url)

    def test_onthemarket_stops_at_seven_days(self):
        """Its ceiling is the lowest of the three: recently-added=14-days
        measured ZERO results, so past 7 days it must not be scoped."""
        cfg = cfg_with_state(self.tmp, days_ago=8, searches=(OTM,))
        url, note = fetch.scope_search_url(OTM, cfg, 8)
        self.assertNotIn("recently-added", url)
        self.assertIn("not scoped", note)

    def test_rightmove_uses_its_own_spelling(self):
        cfg = cfg_with_state(self.tmp, days_ago=0, searches=(RIGHTMOVE,))
        url, _ = fetch.scope_search_url(RIGHTMOVE, cfg, 0)
        self.assertIn("maxDaysSinceAdded=3", url)

    def test_rightmove_stops_at_fourteen_days(self):
        """Rightmove offers no window past 14, and an unsupported value is not
        ignored - maxDaysSinceAdded=30 measured ZERO results, which this
        pipeline reads as a possible block. Past 14 it must not be scoped."""
        cfg = cfg_with_state(self.tmp, days_ago=20, searches=(RIGHTMOVE,))
        url, note = fetch.scope_search_url(RIGHTMOVE, cfg, 20)
        self.assertNotIn("maxDaysSinceAdded", url)
        self.assertIn("not scoped", note)

    def test_the_same_gap_can_scope_portals_differently(self):
        """Three ceilings - 30, 14 and 7 days - so one gap can scope some
        portals and leave others walking their whole backlog."""
        cfg = cfg_with_state(self.tmp, days_ago=10)
        zoopla, _ = fetch.scope_search_url(ZOOPLA, cfg, 10)
        rm, _ = fetch.scope_search_url(RIGHTMOVE, cfg, 10)
        otm, _ = fetch.scope_search_url(OTM, cfg, 10)
        self.assertIn("added=14_days", zoopla)
        self.assertIn("maxDaysSinceAdded=14", rm)
        self.assertNotIn("recently-added", otm)

    def test_a_window_already_in_the_url_is_not_doubled(self):
        """Whoever wrote it into the config meant it, and two values for one
        filter is a URL neither of us chose."""
        cfg = cfg_with_state(self.tmp, days_ago=0)
        mine = ZOOPLA + "&added=30_days"
        url, note = fetch.scope_search_url(mine, cfg, 0)
        self.assertEqual(url, mine)
        self.assertIn("left alone", note)

    def test_an_unreadable_timestamp_reads_as_a_first_run(self):
        cfg = make_config(self.tmp)
        cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.state_path.write_text(json.dumps({"updated": "last tuesday"}),
                                  encoding="utf-8")
        self.assertIsNone(fetch.days_since_last_run(cfg))


class TestTheParameterIsAppendedAsAQueryParameter(unittest.TestCase):
    """Every search URL in the config today carries a query string, so
    appending "&param=value" happens to work. A path-only URL - which both
    OnTheMarket and OpenRent will produce - would get an ampersand inside its
    path instead, and neither failure says anything: the portal either ignores
    the window and pages its whole backlog, or 404s and returns nothing.
    """

    def test_a_url_that_already_has_a_query_string_gets_an_ampersand(self):
        self.assertEqual(fetch.add_param("https://x.test/london?beds=2", "added", "3_days"),
                         "https://x.test/london?beds=2&added=3_days")

    def test_a_path_only_url_gets_a_question_mark(self):
        self.assertEqual(fetch.add_param("https://x.test/to-rent/london", "added", "3_days"),
                         "https://x.test/to-rent/london?added=3_days")

    def test_the_scoped_url_is_still_fetchable(self):
        """End to end through scope_search_url, not just the helper."""
        cfg = make_config(first_run_days=3)
        url, _ = fetch.scope_search_url(
            "https://www.onthemarket.com/to-rent/property/london/", cfg, None)
        self.assertIn("?recently-added=3-days", url)
        self.assertNotIn("/london/&", url)


class TestNoAvailabilityFilterIsEverAsked(unittest.TestCase):
    """A move-in deadline is filtered here, never at the portal.

    This module once appended an availability filter of its own whenever
    `move_in` was set, behind a `verify_availability_by_search` switch, and
    later read one back off the URL as evidence. All of it is gone. The reason
    is in the counts: a portal asked for an availability date also drops every
    listing it cannot date - Zoopla returns 6139 of 8469 at its widest window,
    and widening from three months to twelve adds 28 listings, so the ~2330
    missing are not late, they are undated. Rejecting on missing data is the
    one thing this pipeline does not do.

    The recency window above is still asked for, and the difference is the
    rule: the run asks the portal only for what the URL cannot say - days since
    the last run, different every morning. A move-in date is not.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def scoped(self, url, **over):
        cfg = cfg_with_state(self.tmp, days_ago=0, searches=(url,), **over)
        return fetch.scope_search_url(url, cfg, 0)[0]

    def test_a_move_in_date_does_not_touch_a_zoopla_url(self):
        out = self.scoped(ZOOPLA, move_in=(dt.date.today()
                                           + dt.timedelta(days=93)).isoformat())
        self.assertNotIn("available_from", out)

    def test_a_move_in_date_does_not_touch_a_rightmove_url(self):
        out = self.scoped(RIGHTMOVE, move_in=(dt.date.today()
                                              + dt.timedelta(days=93)).isoformat())
        self.assertNotIn("moveInByDate", out)

    def test_a_move_in_date_does_not_touch_an_openrent_url(self):
        out = self.scoped(OPENRENT, move_in=(dt.date.today()
                                             + dt.timedelta(days=93)).isoformat())
        self.assertNotIn("availableBefore", out)

    def test_the_recency_window_is_still_asked_for(self):
        """The distinction the removal rests on: this one the URL cannot
        state, because it changes every morning."""
        self.assertIn("added=", self.scoped(ZOOPLA, first_run_days=3))


class TestTheMoveInFilterRunsHere(unittest.TestCase):
    """A stated date past the window rejects, like a stated size below
    the floor. An absent one never does."""

    def cfg(self, **over):
        return make_config(move_in="2026-12-01", move_in_slack_days=7, **over)

    def listing(self, **kw):
        base = {"postcode": "SW3 1AA", "price_pcm": 2000,
                "bed_count": 2, "bathrooms": 2, "size_sqft": 900}
        base.update(kw)
        return base

    def test_a_date_past_the_window_is_rejected(self):
        self.assertEqual(
            core.hard_filter(self.listing(available_from="2027-06-01"), self.cfg()),
            "available 2027-06-01, after 2026-12-08")

    def test_a_date_inside_the_slack_survives(self):
        """Slack is part of the window, not a grace note."""
        self.assertIsNone(
            core.hard_filter(self.listing(available_from="2026-12-08"), self.cfg()))

    def test_a_date_before_the_move_in_survives(self):
        self.assertIsNone(
            core.hard_filter(self.listing(available_from="2026-10-01"), self.cfg()))

    def test_no_stated_date_never_rejects(self):
        """The whole reason this is not done at the portal."""
        self.assertIsNone(core.hard_filter(self.listing(), self.cfg()))

    def test_an_unreadable_date_never_rejects(self):
        self.assertIsNone(
            core.hard_filter(self.listing(available_from="Ask agent"), self.cfg()))

    def test_without_a_move_in_date_nothing_is_rejected_for_timing(self):
        self.assertIsNone(
            core.hard_filter(self.listing(available_from="2028-01-01"),
                             make_config()))

    def test_a_surviving_date_is_noted_for_the_digest(self):
        _, notes = core.base_tier(self.listing(available_from="2026-10-01"),
                                  self.cfg())
        self.assertIn("available 2026-10-01", notes)

    def test_silence_still_says_it_is_an_assumption(self):
        _, notes = core.base_tier(self.listing(), self.cfg())
        self.assertIn("availability not stated (assumed available now)", notes)


class TestWindowsDoNotTripTheDriftCheck(unittest.TestCase):
    """`config_url_drift` warns when a URL is tighter than the config. A window
    IS deliberately tighter, in a dimension the config has no threshold for, so
    it must stay silent - otherwise every scoped run cries wolf."""

    def test_a_windowed_url_reports_no_drift(self):
        cfg = make_config(searches=(
            ZOOPLA + "&added=3_days&available_from=1months",))
        self.assertEqual(fetch.config_url_drift(cfg), [])


if __name__ == "__main__":
    unittest.main()
