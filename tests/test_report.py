"""What `report` has to make obvious without anyone opening state.json.

On 2026-09-20 it printed `aircon unchecked=58` as a single tally over every
listing tracked, and the obvious reading - 58 detail pages still to fetch -
was wrong. Every one of them belonged to a listing the hard filter had already
thrown out before its page was ever read. The number was correct and the line
was unreadable, so the reason had to be dug out of state.json by hand, after
an answer had already been given from the shape of the runbook's wording
rather than from the data.

The other half is the baseline. Every count here is a running total, so "22
disqualified by their detail page" cannot be told from 22 that were already
there. A snapshot taken before the run is what turns a total into a change.
"""
import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import core  # noqa: E402
from helpers import make_config  # noqa: E402


def listing(status="NEW", aircon="unstated", priority="High", **kw):
    row = {"url": "https://example.com/%d" % id(kw), "status": status,
           "aircon": aircon, "priority": priority, "reported_on": "2026-09-20"}
    row.update(kw)
    return row


class ReportCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(self.tmp.name)

    def write(self, rows):
        for i, r in enumerate(rows):
            r["url"] = "https://example.com/%d" % i
        core.write_json(self.cfg.state_path, {"updated": None, "listings": rows})

    def report(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            core.report(self.cfg)
        return buf.getvalue()


class TestLiveAndRuledOutAreSeparate(ReportCase):

    def test_unchecked_on_a_ruled_out_listing_is_not_a_backlog(self):
        """The exact 2026-09-20 shape: 37 unchecked, every one already
        rejected. The report has to say so itself."""
        self.write([listing() for _ in range(3)] +
                   [listing(status="REJECTED", aircon="unchecked", priority="Low")
                    for _ in range(37)])
        out = self.report()
        self.assertIn("already ruled out", out)
        self.assertIn("nothing to fetch", out)
        self.assertNotIn("unread detail page", out)

    def test_an_unchecked_live_listing_is_reported_as_work_to_do(self):
        self.write([listing(aircon="unchecked"), listing()])
        out = self.report()
        self.assertIn("1 live listing(s) have an unread detail page", out)
        self.assertNotIn("nothing to fetch", out)

    def test_the_aircon_tally_is_split(self):
        self.write([listing(aircon="yes")] +
                   [listing(status="REJECTED", aircon="unchecked")])
        out = self.report()
        line = next(l for l in out.splitlines() if "aircon" in l and "live" in l)
        self.assertIn("yes=1", line)
        self.assertNotIn("unchecked", line)

    def test_a_tracker_with_nothing_ruled_out_prints_no_empty_half(self):
        """The split is there to answer a question, not to double the output."""
        self.write([listing(), listing()])
        tallies = [l for l in self.report().splitlines() if "ruled out" in l
                   and not l.startswith("tracked")]
        self.assertEqual(tallies, [])


class TestBaselineDeltas(ReportCase):

    def test_no_baseline_says_so_rather_than_printing_nothing(self):
        """Printing nothing reads as "nothing changed". It is not: it means
        the totals have nothing to be compared against, and the digest then
        differences them by hand - which is how the run of 2026-09-21 reported
        18 rejections from earlier mornings as new ones."""
        self.write([listing()])
        out = self.report()
        self.assertIn("NO BASELINE", out)
        self.assertNotIn("+1 tracked", out)

    def test_a_baseline_from_another_day_is_marked_stale(self):
        """It differences today against the wrong morning, and the line is
        otherwise indistinguishable from a good one."""
        self.write([listing()])
        core.write_baseline(self.cfg)
        path = self.cfg.runs_dir / "baseline.json"
        snap = core.read_json(path)
        snap["taken_at"] = "2026-01-01T09:00:00"
        core.write_json(path, snap)
        self.write([listing(), listing()])
        out = self.report()
        self.assertIn("STALE", out)
        self.assertIn("+1 tracked", out)

    def test_the_delta_names_what_this_run_changed(self):
        self.write([listing(), listing()])
        core.write_baseline(self.cfg)
        self.write([listing(), listing(),
                    listing(status="REJECTED", aircon="unchecked")])
        out = self.report()
        self.assertIn("+1 tracked", out)
        self.assertIn("+1 ruled out", out)

    def test_newly_confirmed_aircon_shows_up(self):
        self.write([listing(aircon="unchecked")])
        core.write_baseline(self.cfg)
        self.write([listing(aircon="yes")])
        self.assertIn("+1 A/C in unit", self.report())

    def test_an_unchanged_run_says_so_rather_than_printing_zeroes(self):
        self.write([listing()])
        core.write_baseline(self.cfg)
        self.assertIn("nothing changed", self.report())

    def test_filling_a_missing_baseline_does_not_move_today_s(self):
        """`search` is also the repair command. A repair that reset the
        baseline would make the delta cover the repair, not the morning."""
        self.write([listing()])
        first = core.write_baseline(self.cfg)
        self.write([listing(), listing()])
        kept = core.ensure_baseline(self.cfg)
        self.assertEqual(kept["taken_at"], first["taken_at"])
        self.assertEqual(kept["tracked"], 1)

    def test_a_baseline_from_an_earlier_day_is_replaced_not_kept(self):
        self.write([listing()])
        core.write_baseline(self.cfg)
        path = self.cfg.runs_dir / "baseline.json"
        snap = core.read_json(path)
        snap["taken_at"] = "2026-01-01T09:00:00"
        core.write_json(path, snap)
        self.write([listing(), listing()])
        self.assertEqual(core.ensure_baseline(self.cfg)["tracked"], 2)

    def test_the_baseline_is_a_snapshot_not_a_live_read(self):
        """It has to be taken before the run, and survive the run unchanged -
        otherwise the delta is always zero."""
        self.write([listing()])
        snap = core.write_baseline(self.cfg)
        self.write([listing(), listing()])
        self.assertEqual(core.read_baseline(self.cfg)["tracked"], snap["tracked"])
        self.assertEqual(snap["tracked"], 1)


if __name__ == "__main__":
    unittest.main()
