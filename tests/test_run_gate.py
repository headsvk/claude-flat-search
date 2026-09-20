"""`run` must not commit while a model still has listings to read.

The pipeline's other refusal - never commit after a failed search - is well
covered, because it is loud. This one is quiet, and it shipped: `judge.run`
returned None instead of a count, `if pending:` was therefore never true, and
the run of 2026-09-20 sailed past the stop-and-ask banner to commit 43
unjudged listings as `unchecked` and write them into that day's file. A
listing appears in exactly one daily file ever, so the judged version could
never be shown; only `finish` happening to run the same morning repaired it.

These tests drive `cmd_run` with the stages stubbed, because what is being
pinned down is the ordering decision, not the stages.
"""
import argparse
import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import cli  # noqa: E402
from helpers import MINIMAL_TOML, write_config  # noqa: E402


class TestRunStopsForJudgement(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.pending = 0

        stage1 = pathlib.Path(self.tmp.name) / "stage1.json"
        patches = {
            "stage_search": lambda cfg, args: (self.calls.append("search"), stage1)[1],
            "stage_audit": lambda cfg, s: self.calls.append("audit"),
            "stage_details": lambda cfg, s, args: self.calls.append("details"),
            "stage_judge": lambda cfg, s: (self.calls.append("judge"), self.pending)[1],
            "stage_commit": lambda cfg, s, args: self.calls.append("commit"),
            "stage_render": lambda cfg: self.calls.append("render"),
        }
        for name, fn in patches.items():
            real = getattr(cli, name)
            setattr(cli, name, fn)
            self.addCleanup(setattr, cli, name, real)

        real_report = cli.core.report
        cli.core.report = lambda cfg: self.calls.append("report")
        self.addCleanup(setattr, cli.core, "report", real_report)

        self.args = argparse.Namespace(
            config=write_config(self.tmp.name, MINIMAL_TOML),
            incremental=False, refresh=False, cap=None, host=None)

    def run_cmd(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.cmd_run(self.args)

    def test_pending_judgement_stops_before_commit(self):
        self.pending = 43
        self.assertEqual(self.run_cmd(), 0)
        self.assertNotIn("commit", self.calls)
        self.assertNotIn("render", self.calls)

    def test_nothing_pending_goes_on_to_commit_and_render(self):
        self.pending = 0
        self.run_cmd()
        self.assertEqual(self.calls,
                         ["search", "audit", "details", "judge",
                          "commit", "render", "report"])

    def test_one_pending_listing_is_enough_to_stop(self):
        """The boundary. A single unread listing is still a daily file entry
        spent on a stub."""
        self.pending = 1
        self.run_cmd()
        self.assertNotIn("commit", self.calls)

    def test_a_stage_that_forgets_to_return_a_count_is_caught(self):
        """The actual bug: None is falsy, so a stage that silently stops
        reporting its count reopens the hole. Guarding the type is what makes
        the failure loud instead of a quiet commit."""
        cli.stage_judge = lambda cfg, s: (self.calls.append("judge"), None)[1]
        with self.assertRaises(cli.Abort):
            self.run_cmd()
        self.assertNotIn("commit", self.calls)

    def test_the_baseline_is_taken_before_anything_runs(self):
        """So the report at the end can say what this run changed."""
        taken = []
        real = cli.core.write_baseline
        cli.core.write_baseline = lambda cfg: taken.append(len(self.calls))
        self.addCleanup(setattr, cli.core, "write_baseline", real)
        self.run_cmd()
        self.assertEqual(taken, [0])


if __name__ == "__main__":
    unittest.main()
