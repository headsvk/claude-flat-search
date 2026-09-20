"""Three fixes for bugs that were invisible by construction.

Every case here is a defect the code shipped, and each one was invisible for
the same structural reason: the failure produced a tracker that looked
perfectly healthy. That is this project's characteristic bug, so these are the
tests that have to exist.

  * `commit` accepted an `update` flag and never read it, so a morning run
    wrote `unchecked` over a listing's confirmed A/C verdict and erased the
    quote backing it. A destroyed verdict looks exactly like a listing nobody
    has read yet.
  * `apply_aircon` tested `mode == "ignore"` while the only spelling the config
    accepts is `"ignored"`, so the mode was unreachable and behaved as
    `preferred`. Asking for A/C to be ignored still re-ranked on it.
  * `audit.report` returned None rather than True on an empty cache, so every
    fresh clone's first `run` aborted claiming an extractor was broken.
"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import audit, core  # noqa: E402
from helpers import make_config  # noqa: E402


def quiet(fn, *args, **kw):
    """Run fn with stdout swallowed; these functions all report as they go."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


JUDGED = {
    "url": "https://example.com/1",
    "title": "Old title",
    "status": "SHORTLIST",
    "platform": "Rightmove",
    "aircon": "yes",
    "aircon_scope": "in_unit",
    "aircon_evidence": "air conditioning throughout",
    "aircon_checked": "2026-09-01",
    "size_sqft": 1200,
    "priority": "High",
    "found_on": "2026-09-01",
    "last_seen": "2026-09-01",
    "reported_on": "2026-09-01",
}


class TestCommitPreservesWhatItDidNotRead(unittest.TestCase):
    """A run that never opened the page may not overwrite what did."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp, budget_pcm=5000, districts_only=False)
        quiet(core.save_state, self.cfg, {"updated": None,
                                          "listings": [dict(JUDGED)]})
        # Today's search sees the same listing again. Its detail page is not
        # re-fetched - nothing is cached - so this run has no verdict for it.
        self.stage1 = self.tmp / "stage1.json"
        self.stage1.write_text(json.dumps({"run_date": "2026-09-20", "listings": [
            {"url": JUDGED["url"], "title": "Old title", "platform": "Rightmove",
             "price_pcm": 4000, "bed_count": 2, "bathrooms": 2,
             "postcode": "SW3 1AA"}]}), encoding="utf-8")

    def record(self):
        return core.load_state(self.cfg)["listings"][0]

    def test_verdict_and_evidence_survive_a_run_that_did_not_read_the_page(self):
        quiet(core.commit, self.cfg, self.stage1, None, update=False)
        row = self.record()
        self.assertEqual(row["aircon"], "yes")
        self.assertEqual(row["aircon_scope"], "in_unit")
        self.assertEqual(row["aircon_evidence"], "air conditioning throughout")
        self.assertEqual(row["aircon_checked"], "2026-09-01")

    def test_a_measured_size_is_not_blanked_by_a_search_card_that_omits_it(self):
        quiet(core.commit, self.cfg, self.stage1, None, update=False)
        self.assertEqual(self.record()["size_sqft"], 1200)

    def test_priority_stays_consistent_with_the_carried_verdict(self):
        """The carried verdict has to reach the tiering, not just the record.

        Preserving `aircon` while recomputing `priority` without it leaves a
        row that says A/C in unit and is ranked as though it does not - which
        is worse than either answer alone, because nothing looks wrong.
        """
        quiet(core.commit, self.cfg, self.stage1, None, update=False)
        row = self.record()
        self.assertEqual(row["priority"], "High")
        self.assertIn("A/C in unit", row["notes"])

    def test_fresh_values_are_still_written(self):
        """Merging is not freezing: what this run did learn must land."""
        quiet(core.commit, self.cfg, self.stage1, None, update=False)
        row = self.record()
        self.assertEqual(row["price_pcm"], 4000)
        self.assertEqual(row["last_seen"], "2026-09-20")

    def test_status_and_stamps_are_never_touched(self):
        quiet(core.commit, self.cfg, self.stage1, None, update=False)
        row = self.record()
        self.assertEqual(row["status"], "SHORTLIST")
        self.assertEqual(row["found_on"], "2026-09-01")
        self.assertEqual(row["reported_on"], "2026-09-01")

    def test_refresh_rewrites_in_full(self):
        """--refresh means you deliberately re-read the page: the read wins."""
        quiet(core.commit, self.cfg, self.stage1, None, update=True)
        row = self.record()
        self.assertEqual(row["aircon"], "unchecked")
        self.assertEqual(row["aircon_evidence"], "")
        self.assertIsNone(row["size_sqft"])

    def test_a_real_finding_overwrites_a_stored_verdict(self):
        """`unstated` is a reading, not the absence of one, and must win.

        This is the line the fix must not blur. A page re-read and found to
        say nothing about cooling has to clear a previous `yes`; only
        `unchecked` - never having looked - is held back.
        """
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cfg.cache_dir / core.cache_name(JUDGED["url"])
        cache_file.write_text(json.dumps(
            {"url": JUDGED["url"], "fetched_at": "2026-09-20",
             "text": "A lovely flat. Description: no mention of cooling."}),
            encoding="utf-8")
        verdicts = self.tmp / "verdicts.json"
        verdicts.write_text(json.dumps({"verdicts": [
            {"url": JUDGED["url"],
             "aircon": {"verdict": "unstated", "scope": "", "evidence": ""}}]}),
            encoding="utf-8")

        quiet(core.commit, self.cfg, self.stage1, verdicts, update=False)
        row = self.record()
        self.assertEqual(row["aircon"], "unstated")
        self.assertEqual(row["aircon_evidence"], "")

    def test_carry_verdict_refuses_a_value_that_is_not_a_verdict(self):
        """A junk `aircon` on a hand-edited record must not be carried."""
        for stored in ("", None, "Unknown", "unchecked", "maybe", "YES please"):
            with self.subTest(stored=stored):
                fresh = {"verdict": "unchecked", "scope": "", "evidence": "",
                         "checked": "", "flags": []}
                out = core.carry_verdict(fresh, {"aircon": stored})
                self.assertEqual(out["verdict"], "unchecked")


class TestAirconModeIgnored(unittest.TestCase):
    """`ignored` is a mode the config accepts; it has to do something."""

    HIT = {"verdict": "yes", "scope": "in_unit", "evidence": "air conditioning"}

    def test_ignored_neither_promotes_nor_annotates(self):
        cfg = make_config(aircon="ignored")
        self.assertEqual(core.apply_aircon(1, self.HIT, cfg), (1, []))

    def test_ignored_does_not_demote_a_listing_with_no_aircon(self):
        cfg = make_config(aircon="ignored")
        unchecked = {"verdict": "unchecked", "scope": "unclear"}
        self.assertEqual(core.apply_aircon(0, unchecked, cfg), (0, []))

    def test_preferred_still_promotes(self):
        cfg = make_config(aircon="preferred")
        tier, notes = core.apply_aircon(1, self.HIT, cfg)
        self.assertEqual(tier, 0)
        self.assertIn("A/C in unit", notes)

    def test_required_demotes_an_unconfirmed_listing(self):
        cfg = make_config(aircon="required")
        tier, notes = core.apply_aircon(0, {"verdict": "unstated"}, cfg)
        self.assertEqual(tier, 2)
        self.assertTrue(any("not confirmed" in n for n in notes))

    def test_required_holds_the_tier_for_a_page_never_read(self):
        cfg = make_config(aircon="required")
        tier, notes = core.apply_aircon(0, {"verdict": "unchecked"}, cfg)
        self.assertEqual(tier, 0)
        self.assertTrue(any("not yet checked" in n for n in notes))

    def test_every_mode_the_config_accepts_has_its_own_behaviour(self):
        """The bug was a spelling drift between two files: `core` branched on
        "ignore", `config` only ever produced "ignored". Neither file is wrong
        on its own, which is why reading either one alone finds nothing.

        So assert against config.AIRCON_MODES itself, one documented outcome
        per mode. A mode that stops matching its branch now falls into another
        mode's row and fails, instead of silently becoming `preferred`.
        """
        from flatsearch import config as config_mod

        expected = {
            # mode      -> (tier from 1 on an in-unit hit, notes non-empty)
            "preferred": (0, True),    # promotes, and says why
            "required":  (1, False),   # already satisfied: holds, says nothing
            "ignored":   (1, False),   # no-op in both directions
        }
        self.assertEqual(set(expected), set(config_mod.AIRCON_MODES),
                         "a mode was added to the config with no behaviour "
                         "pinned here")

        for mode, (tier_want, noted) in expected.items():
            with self.subTest(mode=mode):
                cfg = make_config(aircon=mode)
                tier, notes = core.apply_aircon(1, self.HIT, cfg)
                self.assertEqual(tier, tier_want)
                self.assertEqual(bool(notes), noted)


class TestAuditOnAFreshClone(unittest.TestCase):
    """Nothing cached yet is the starting state, not a broken extractor."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stage1 = self.tmp / "stage1.json"
        self.stage1.write_text(json.dumps({"run_date": "2026-09-20", "listings": [
            {"url": "https://www.rightmove.co.uk/properties/1",
             "platform": "Rightmove", "price_pcm": 2500, "bed_count": 2,
             "bathrooms": 2, "postcode": "SW3 1AA"}]}), encoding="utf-8")

    def test_empty_cache_certifies_ready(self):
        self.assertIs(quiet(audit.report, self.cfg, self.stage1), True)

    def test_run_gets_past_the_audit_gate_on_a_first_run(self):
        from flatsearch import cli
        try:
            quiet(cli.stage_audit, self.cfg, self.stage1)
        except cli.Abort as exc:
            self.fail("a first run cannot start: %s" % exc)

    def test_a_genuinely_broken_extractor_still_fails(self):
        """The gate must still bite once there is something to judge."""
        cache_file = self.cfg.cache_dir / "aaaaaaaaaaaaaaaa.json"
        cache_file.write_text(json.dumps(
            {"url": "https://www.rightmove.co.uk/properties/1",
             "fetched_at": "2026-09-20", "text": "tiny"}), encoding="utf-8")
        self.assertIs(quiet(audit.report, self.cfg, self.stage1), False)

    def test_a_cache_file_without_a_url_is_skipped_not_crashed_on(self):
        (self.cfg.cache_dir / "bbbbbbbbbbbbbbbb.json").write_text(
            json.dumps({"fetched_at": "2026-09-20", "text": "x" * 900}),
            encoding="utf-8")
        self.assertIs(quiet(audit.report, self.cfg, self.stage1), True)


if __name__ == "__main__":
    unittest.main()
