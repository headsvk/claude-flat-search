"""A listing is tracked only once its detail page has been read.

The fetch skips tracked listings, so a listing tracked unread is never read.
Until 2026-09-25 that is exactly what happened past the 150-page cap: the
overflow was committed as tracked, every later run skipped it, and 139 listings
sat unread until the 7-day release put them out as stubs. `--refresh` could not
reach them either, once they had aged out of the search window.

Now an unread listing is parked in state `pending` with its search row, and
`plan` queues it again every run until it is read or has waited HOLD_DAYS.
"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import config as config_mod  # noqa: E402
from flatsearch import core  # noqa: E402
from helpers import MINIMAL_TOML, make_config, write_config  # noqa: E402


def quiet(fn, *args, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


def row(n=1, **kw):
    r = {"url": "https://www.rightmove.co.uk/properties/%d" % n,
         "title": "2 bed flat %d" % n, "platform": "Rightmove",
         "price_pcm": 2800, "bed_count": 2, "bathrooms": 2,
         "postcode": "NW8 1AA", "area": "NW8", "size_sqft": 950}
    r.update(kw)
    return r


class PendingCase(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stage1 = self.cfg.runs_dir / "stage1.json"
        self.stage1.parent.mkdir(parents=True, exist_ok=True)

    def search(self, rows, day):
        core.write_json(self.stage1, {"run_date": day, "listings": rows})

    def read_page(self, url):
        core.write_json(self.cfg.cache_dir / core.cache_name(url),
                        {"url": url, "text": "A bright flat. " * 30})

    def plan(self):
        return quiet(core.plan, self.cfg, self.stage1,
                     self.cfg.runs_dir / "queue.json")

    def commit(self):
        quiet(core.commit, self.cfg, self.stage1, None)
        return core.load_state(self.cfg)

    def tracked(self, state):
        return {l["url"]: l for l in state["listings"]}


class TestNoCap(PendingCase):

    def test_every_uncached_listing_is_queued(self):
        self.search([row(n) for n in range(1, 401)], "2026-09-25")
        self.assertEqual(len(self.plan()["fetch"]), 400)


class TestUnreadIsNotTracked(PendingCase):

    def test_an_unread_listing_is_parked_not_tracked(self):
        self.search([row(1)], "2026-09-25")
        state = self.commit()
        self.assertEqual(state["listings"], [])
        self.assertIn(row(1)["url"], state["pending"])

    def test_a_read_listing_is_tracked(self):
        self.search([row(1)], "2026-09-25")
        self.read_page(row(1)["url"])
        state = self.commit()
        self.assertIn(row(1)["url"], self.tracked(state))
        self.assertEqual(state["pending"], {})

    def test_a_parked_listing_is_queued_after_it_leaves_the_search(self):
        """The case --refresh could not reach: not in today's results."""
        self.search([row(1)], "2026-09-25")
        self.commit()
        self.search([row(2)], "2026-09-26")
        queued = {q["url"] for q in self.plan()["fetch"]}
        self.assertEqual(queued, {row(1)["url"], row(2)["url"]})

    def test_a_retried_listing_is_tracked_once_read_and_keeps_its_dates(self):
        self.search([row(1)], "2026-09-25")
        self.commit()
        self.search([row(2)], "2026-09-26")
        self.plan()                       # appends the retry row to stage1
        self.read_page(row(1)["url"])
        self.read_page(row(2)["url"])
        got = self.tracked(self.commit())
        self.assertEqual(got[row(1)["url"]]["found_on"], "2026-09-25")
        # Found by a retry, not by today's search: not seen today.
        self.assertEqual(got[row(1)["url"]]["last_seen"], "2026-09-25")
        self.assertEqual(got[row(2)["url"]]["found_on"], "2026-09-26")
        self.assertEqual(core.load_state(self.cfg)["pending"], {})

    def test_a_retried_listing_is_hard_filtered_like_any_other(self):
        """Its page can disqualify it; it is then dropped, not parked."""
        self.search([row(1)], "2026-09-25")
        self.commit()
        self.search([], "2026-09-26")
        self.plan()
        self.read_page(row(1)["url"])
        stage1 = core.read_json(self.stage1)
        stage1["listings"][0]["size_sqft"] = 500      # what the page said
        core.write_json(self.stage1, stage1)
        state = self.commit()
        self.assertEqual(state["listings"], [])
        self.assertEqual(state["pending"], {})

    def test_it_is_released_unread_after_hold_days(self):
        """A page that never loads must still surface eventually."""
        self.search([row(1)], "2026-09-18")
        self.commit()
        self.search([row(1)], "2026-09-25")
        got = self.tracked(self.commit())
        rec = got[row(1)["url"]]
        self.assertEqual(rec["found_on"], "2026-09-18")
        self.assertEqual(rec["aircon"], "unchecked")
        self.assertIn("detail page never loaded (2 attempts)", rec["notes"])
        self.assertEqual(core.load_state(self.cfg)["pending"], {})

    def test_a_second_commit_the_same_day_is_not_a_second_attempt(self):
        """`finish` re-commits the morning; that is not another failed fetch."""
        self.search([row(1)], "2026-09-25")
        self.commit()
        state = self.commit()
        self.assertEqual(state["pending"][row(1)["url"]]["attempts"], 1)

    def test_it_waits_while_inside_hold_days(self):
        self.search([row(1)], "2026-09-19")
        self.commit()
        self.search([row(1)], "2026-09-25")
        state = self.commit()
        self.assertEqual(state["listings"], [])
        self.assertEqual(state["pending"][row(1)["url"]]["attempts"], 2)


class TestDailyFileCountsThem(unittest.TestCase):

    def test_pending_listings_are_counted_as_waiting(self):
        from flatsearch import render
        read = {"url": "https://example.com/1", "title": "2 bed", "status": "NEW",
                "priority": "High", "aircon": "unstated",
                "aircon_checked": "2026-09-25", "found_on": "2026-09-25",
                "reported_on": None}
        state = {"listings": [read],
                 "pending": {"https://example.com/2": {"first_seen": "2026-09-24"}}}
        text = render.render_daily(state, {}, "2026-09-25")[0]
        self.assertIn("1 more are waiting on a detail page", text)
        self.assertNotIn("found today", text)


class TestRemovedKey(unittest.TestCase):

    def test_stage2_cap_says_it_was_removed(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = write_config(d.name, MINIMAL_TOML + "\n[run]\nstage2_cap = 150\n")
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.load(path)
        self.assertIn("stage2_cap has been removed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
