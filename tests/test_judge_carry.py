"""Judgement carried forward, and the return value `run` is steered by.

Two bugs live here, both of which shipped.

The first is a missing `return`. `judge.run` is declared `-> int` and
`flat-search run` branches on it to decide whether to stop and ask a model or
go on and commit. Falling off the end returned None, the branch was dead, and
the run of 2026-09-20 committed 43 unjudged listings as `unchecked` and wrote
them into a daily file - which a listing only ever gets one of.

The second is that `run` scans the whole cache, so a listing that mentions
cooling is a candidate every morning for as long as it is tracked. 43 flagged,
4 genuinely new; the other 39 were diffed out by hand in the session. These
pin down the automatic version, including the case the shortcut must not
swallow: a page rewritten under a stored verdict.
"""
import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import core, judge  # noqa: E402
from helpers import make_config  # noqa: E402

AC_TEXT = ("A superb two bedroom apartment. " * 8 +
           "The property benefits from air conditioning throughout. " +
           "Further details on request. " * 4)
PLAIN_TEXT = "A superb two bedroom apartment with a private balcony. " * 10


class JudgeCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(self.tmp.name)
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    def cache(self, url, text):
        core.write_json(self.cfg.cache_dir / core.cache_name(url),
                        {"url": url, "text": text, "fetched_at": "2026-09-20"})

    def haiku(self, *verdicts):
        core.write_json(self.cfg.runs_dir / "verdicts_haiku.json",
                        {"verdicts": list(verdicts)})

    def judge(self):
        out = self.cfg.runs_dir / "verdicts.json"
        review = self.cfg.runs_dir / "needs_review.json"
        # The stage narrates itself to a human running it by hand; a test suite
        # is not that, and the noise buries a real failure.
        with contextlib.redirect_stdout(io.StringIO()):
            pending = judge.run(self.cfg, out, review)
        return pending, core.read_json(out)["verdicts"], \
            core.read_json(review)["needs_model_judgement"]


class TestReturnValue(JudgeCase):
    """The value `flat-search run` gates the whole commit on."""

    def test_returns_the_number_needing_a_model(self):
        self.cache("https://example.com/1", AC_TEXT)
        self.cache("https://example.com/2", AC_TEXT)
        self.cache("https://example.com/3", PLAIN_TEXT)
        pending, _, review = self.judge()
        self.assertEqual(pending, 2)
        self.assertEqual(len(review), 2)

    def test_returns_zero_when_nothing_mentions_cooling(self):
        """Zero, not None. `if pending:` cannot tell them apart, which is
        exactly how the bug stayed invisible."""
        self.cache("https://example.com/1", PLAIN_TEXT)
        pending, _, _ = self.judge()
        self.assertEqual(pending, 0)
        self.assertIsNotNone(pending)


class TestCarryForward(JudgeCase):

    def stored(self, url, text, **over):
        v = {"url": url,
             "aircon": {"verdict": "yes", "scope": "in_unit",
                        "evidence": "air conditioning throughout"},
             "text_sha": judge.text_sha(text)}
        v.update(over)
        return v

    def test_a_judged_listing_is_not_flagged_again(self):
        url = "https://example.com/1"
        self.cache(url, AC_TEXT)
        self.haiku(self.stored(url, AC_TEXT))
        pending, verdicts, review = self.judge()
        self.assertEqual(pending, 0)
        self.assertEqual(review, [])

    def test_the_carried_verdict_lands_in_verdicts_json(self):
        """So a morning with nothing new commits the real verdict without
        waiting for `finish`. Before this it committed `unchecked`."""
        url = "https://example.com/1"
        self.cache(url, AC_TEXT)
        self.haiku(self.stored(url, AC_TEXT))
        _, verdicts, _ = self.judge()
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["url"], url)
        self.assertEqual(verdicts[0]["aircon"]["verdict"], "yes")
        self.assertEqual(verdicts[0]["aircon"]["scope"], "in_unit")

    def test_only_the_new_one_is_flagged(self):
        old, new = "https://example.com/1", "https://example.com/2"
        self.cache(old, AC_TEXT)
        self.cache(new, AC_TEXT)
        self.haiku(self.stored(old, AC_TEXT))
        pending, _, review = self.judge()
        self.assertEqual(pending, 1)
        self.assertEqual([r["url"] for r in review], [new])

    def test_a_rewritten_page_is_re_judged_not_carried(self):
        """The one case the shortcut must not swallow. `commit` would catch a
        quote that no longer appears, but silently - the listing lands on
        `unstated` and is never read again. Re-flagging is what recovers."""
        url = "https://example.com/1"
        self.cache(url, AC_TEXT)
        self.haiku(self.stored(url, "some entirely different description"))
        pending, _, review = self.judge()
        self.assertEqual(pending, 1)
        self.assertTrue(review[0]["rejudge"])

    def test_a_verdict_with_no_fingerprint_falls_back_to_its_quote(self):
        """Verdicts written before fingerprints existed. The quote is the same
        test `commit` applies, so a still-quotable verdict carries forward."""
        url = "https://example.com/1"
        self.cache(url, AC_TEXT)
        self.haiku({"url": url,
                    "aircon": {"verdict": "yes", "scope": "in_unit",
                               "evidence": "air conditioning throughout"}})
        pending, verdicts, _ = self.judge()
        self.assertEqual(pending, 0)
        self.assertEqual(verdicts[0]["aircon"]["verdict"], "yes")

    def test_an_unfingerprinted_verdict_whose_quote_is_gone_is_re_judged(self):
        url = "https://example.com/1"
        self.cache(url, AC_TEXT)
        self.haiku({"url": url,
                    "aircon": {"verdict": "yes", "scope": "in_unit",
                               "evidence": "cooling in every room"}})
        pending, _, review = self.judge()
        self.assertEqual(pending, 1)

    def test_a_silent_listing_is_still_decided_in_python(self):
        """Carrying verdicts forward must not disturb the cheap path."""
        url = "https://example.com/1"
        self.cache(url, PLAIN_TEXT)
        self.haiku(self.stored(url, PLAIN_TEXT))
        _, verdicts, _ = self.judge()
        self.assertEqual(verdicts[0]["aircon"]["verdict"], "unstated")


class TestStamping(JudgeCase):

    def test_merge_records_which_page_each_verdict_was_read_off(self):
        url = "https://example.com/1"
        self.cache(url, AC_TEXT)
        core.write_json(self.cfg.runs_dir / "verdicts.json", {"verdicts": []})
        self.haiku({"url": url,
                    "aircon": {"verdict": "yes", "scope": "in_unit",
                               "evidence": "air conditioning throughout"}})
        with contextlib.redirect_stdout(io.StringIO()):
            judge.merge_verdicts(self.cfg)
        stored = core.read_json(self.cfg.runs_dir / "verdicts_haiku.json")
        self.assertEqual(stored["verdicts"][0]["text_sha"], judge.text_sha(AC_TEXT))

    def test_the_fingerprint_ignores_whitespace_churn(self):
        """A portal reflowing its markup is not a rewritten description, and
        must not send a settled listing back to the model."""
        self.assertEqual(judge.text_sha(AC_TEXT),
                         judge.text_sha(AC_TEXT.replace(" ", "  ")))


if __name__ == "__main__":
    unittest.main()
