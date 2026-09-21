"""`commit` must refuse while a listing a model was asked to read is unread.

The refusal used to live in `flat-search run`, which is the one path that
cannot reach commit without passing it. `flat-search commit` had none at all,
so every invariant protecting the daily files rested on whoever was driving
the stages having read step 4 of the runbook. On 2026-09-20 that reader was a
bug - `judge.run` returned None - and 43 unjudged listings were committed and
written into a daily file, which a listing only ever gets one of.

The gate is absolute on purpose: there is no flag to get past it. `finish`
merges the model's verdicts first and passes it for free, which is what makes
that the way through.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import core  # noqa: E402
from helpers import make_config  # noqa: E402

FLAGGED = "https://example.com/flagged"


class TestCommitGate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(self.tmp.name)
        self.cfg.runs_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stage1 = self.cfg.runs_dir / "stage1.json"
        core.write_json(self.stage1, {"listings": []})
        self.verdicts = self.cfg.runs_dir / "verdicts.json"

    def review(self, urls):
        core.write_json(self.cfg.runs_dir / "needs_review.json",
                        {"needs_model_judgement": [{"url": u} for u in urls]})

    def wrote(self, urls):
        core.write_json(self.verdicts,
                        {"verdicts": [{"url": u, "aircon": {"verdict": "yes"}}
                                      for u in urls]})

    def commit(self):
        core.commit(self.cfg, self.stage1, self.verdicts)

    def test_an_unread_listing_stops_the_commit(self):
        self.review([FLAGGED])
        self.wrote([])
        with self.assertRaises(core.Abort) as caught:
            self.commit()
        self.assertIn(FLAGGED, str(caught.exception))

    def test_the_verdict_being_present_is_what_clears_it(self):
        """What `finish` does: merge the model's file in, then commit."""
        self.review([FLAGGED])
        self.wrote([FLAGGED])
        self.commit()

    def test_a_morning_with_nothing_flagged_commits(self):
        self.review([])
        self.wrote([])
        self.commit()

    def test_a_leftover_review_file_refuses_rather_than_assuming(self):
        """`judge` rewrites the file every run, so one left from yesterday
        means judgement was skipped entirely this morning."""
        self.review([FLAGGED])
        self.assertFalse(self.verdicts.exists())
        with self.assertRaises(core.Abort):
            core.commit(self.cfg, self.stage1, self.verdicts)

    def test_no_review_file_at_all_is_not_a_refusal(self):
        """A first run, before judge has ever written one."""
        self.wrote([])
        self.commit()


if __name__ == "__main__":
    unittest.main()
