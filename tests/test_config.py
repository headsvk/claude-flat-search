"""Config loading and validation.

The config moved from markdown-with-a-bespoke-parser to TOML. The three things
that cost real bugs under the old scheme are what these pin down:

  * a value's TYPE (everything used to be a string, so the code carried
    cfg_int/cfg_list/truthy helpers to undo that);
  * a typo'd key being a silent default rather than an error - a threshold that
    quietly reverts filters the wrong listings for weeks with nothing looking
    wrong;
  * search URLs being scraped out of the whole file, so a URL written in prose
    as an illustration became a live search. That one actually happened.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from flatsearch import config as C  # noqa: E402
from helpers import write_config  # noqa: E402


class ConfigCase(unittest.TestCase):
    def load(self, body):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return C.load(write_config(d.name, body))

    def error(self, body):
        with self.assertRaises(C.ConfigError) as ctx:
            self.load(body)
        return str(ctx.exception)


MINIMAL = """
[requirements]
budget_pcm = 3000
"""


class TestTypes(ConfigCase):
    """Real types, not strings. This is why cfg_int and cfg_list are gone."""

    def test_numbers_are_ints(self):
        cfg = self.load(MINIMAL + "\n[preferences]\nmin_sqft = 800\n")
        self.assertIsInstance(cfg.min_sqft, int)
        self.assertEqual(cfg.min_sqft, 800)

    def test_flags_are_bools(self):
        cfg = self.load(MINIMAL + "\n[districts]\nonly = true\n")
        self.assertIs(cfg.districts_only, True)

    def test_district_lists_are_tuples_of_lowercase(self):
        cfg = self.load(MINIMAL + '\n[districts]\nprime = ["W1", "SW3"]\n')
        self.assertEqual(cfg.prime_districts, ("w1", "sw3"))

    def test_a_quoted_number_is_rejected(self):
        """The old parser would have accepted "3000" and silently int()'d it."""
        msg = self.error('[requirements]\nbudget_pcm = "3000"\n')
        self.assertIn("whole number", msg)

    def test_true_is_not_a_number(self):
        """bool subclasses int in Python; the loader must not let it through."""
        msg = self.error("[requirements]\nbudget_pcm = true\n")
        self.assertIn("whole number", msg)


class TestValidation(ConfigCase):

    def test_missing_required_key(self):
        self.assertIn("budget_pcm", self.error("[requirements]\nmin_sqft = 1\n"))

    def test_unknown_key_is_an_error_not_a_default(self):
        """THE point of validating. A typo used to be a silent default."""
        msg = self.error(MINIMAL + "\n[preferences]\nmin_sqfeet = 800\n")
        self.assertIn("min_sqfeet", msg)
        self.assertIn("Known keys", msg)

    def test_unknown_section_is_an_error(self):
        self.assertIn("preferenes", self.error(MINIMAL + "\n[preferenes]\n"))

    def test_bad_furnishing_value(self):
        msg = self.error(MINIMAL + '\n[preferences]\nfurnishing = "partly"\n')
        self.assertIn("furnishing", msg)

    def test_bad_aircon_mode(self):
        msg = self.error(MINIMAL + '\n[preferences]\naircon = "yes please"\n')
        self.assertIn("aircon", msg)

    def test_zero_budget_is_rejected(self):
        """A zero budget would pass every listing rather than none."""
        self.assertIn("budget_pcm", self.error("[requirements]\nbudget_pcm = 0\n"))

    def test_negative_value_is_rejected(self):
        self.assertIn("negative", self.error("[requirements]\nbudget_pcm = -5\n"))

    def test_invalid_toml_says_so(self):
        self.assertIn("not valid TOML", self.error("[requirements\nbudget_pcm = 1\n"))

    def test_missing_file_names_the_example(self):
        with self.assertRaises(C.ConfigError) as ctx:
            C.load("no-such-config.toml")
        self.assertIn("criteria.example.toml", str(ctx.exception))

    def test_defaults_apply_when_a_key_is_absent(self):
        cfg = self.load(MINIMAL)
        self.assertEqual(cfg.min_sqft, 0)
        self.assertEqual(cfg.move_in_slack_days, 7)
        self.assertIs(cfg.districts_only, False)


class TestSearches(ConfigCase):
    """URLs live in a table, so a URL is a search only because it is one."""

    BODY = MINIMAL + '''
[searches]
rightmove = [
  "https://www.rightmove.co.uk/a?x=1",
  "https://www.rightmove.co.uk/a?x=2",
]
zoopla = ["https://www.zoopla.co.uk/b?y=1"]
'''

    def test_reads_every_url(self):
        self.assertEqual(len(self.load(self.BODY).searches), 3)

    def test_a_url_in_a_comment_is_not_a_search(self):
        """THE regression this format removes by construction. Under the old
        markdown parser an example URL in prose was fetched and committed."""
        body = self.BODY + '\n# For example: https://www.zoopla.co.uk/example?z=9\n'
        got = self.load(body).searches
        self.assertEqual(len(got), 3)
        self.assertFalse(any("example" in u for u in got))

    def test_a_single_url_need_not_be_a_list(self):
        cfg = self.load(MINIMAL + '\n[searches]\nzoopla = "https://www.zoopla.co.uk/x"\n')
        self.assertEqual(len(cfg.searches), 1)

    def test_duplicates_collapse(self):
        one = "https://www.zoopla.co.uk/x"
        cfg = self.load(MINIMAL + '\n[searches]\na = ["%s"]\nb = ["%s"]\n' % (one, one))
        self.assertEqual(len(cfg.searches), 1)

    def test_a_non_url_is_an_error(self):
        msg = self.error(MINIMAL + '\n[searches]\nzoopla = ["not a url"]\n')
        self.assertIn("not a URL", msg)

    def test_no_searches_is_allowed_at_load(self):
        """`check` should be able to validate a half-written config."""
        self.assertEqual(self.load(MINIMAL).searches, ())


class TestPaths(ConfigCase):

    def test_data_dir_resolves_against_the_config_file(self):
        cfg = self.load(MINIMAL + '\n[run]\ndata_dir = "data"\n')
        self.assertEqual(cfg.data_dir.name, "data")
        self.assertEqual(cfg.data_dir.parent, cfg.path.parent.resolve())

    def test_derived_paths_all_sit_under_data_dir(self):
        cfg = self.load(MINIMAL)
        for p in (cfg.state_path, cfg.cache_dir, cfg.runs_dir, cfg.daily_dir,
                  cfg.shortlist_path, cfg.decisions_path):
            self.assertEqual(p.parent, cfg.data_dir, p)

    def test_absolute_data_dir_is_respected(self):
        root = pathlib.Path(tempfile.gettempdir()).resolve() / "flatsearch-abs"
        cfg = self.load(MINIMAL + '\n[run]\ndata_dir = "%s"\n'
                        % str(root).replace("\\", "\\\\"))
        self.assertEqual(cfg.data_dir, root)


class TestTierOf(ConfigCase):

    def setUp(self):
        self.cfg = self.load(MINIMAL + '''
[districts]
prime    = ["W1", "SW1", "NW8"]
affluent = ["SW6", "N1"]
fringe   = ["E14"]
''')

    def test_tiers(self):
        self.assertEqual(self.cfg.tier_of("NW8"), 0)
        self.assertEqual(self.cfg.tier_of("SW6"), 1)
        self.assertEqual(self.cfg.tier_of("E14"), 2)

    def test_case_insensitive(self):
        self.assertEqual(self.cfg.tier_of("nw8"), 0)

    def test_full_outward_code_stems_to_its_district(self):
        """'Sloane Street, London, SW1X' must reach the prime tier. Without the
        stem fallback SW1X / W1K / WC2R rank as unknown."""
        self.assertEqual(self.cfg.tier_of("SW1X"), 0)

    def test_unlisted_is_none(self):
        self.assertIsNone(self.cfg.tier_of("BR1"))

    def test_none_is_none(self):
        self.assertIsNone(self.cfg.tier_of(None))


class TestShippedExample(unittest.TestCase):
    """examples/criteria.example.toml is what a new user copies."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def setUp(self):
        self.path = self.ROOT / "examples" / "criteria.example.toml"
        if not self.path.exists():
            self.skipTest("example config not present")
        self.cfg = C.load(self.path)

    def test_it_loads(self):
        self.assertGreater(self.cfg.budget_pcm, 0)

    def test_it_has_searches_for_every_portal(self):
        hosts = {u.split("/")[2] for u in self.cfg.searches}
        for portal in ("rightmove", "zoopla", "onthemarket", "openrent"):
            self.assertTrue(any(portal in h for h in hosts), portal)

    def test_its_urls_do_not_contradict_its_thresholds(self):
        """The example must not ship the drift it warns about."""
        from flatsearch import fetch
        self.assertEqual(fetch.config_url_drift(self.cfg), [])


if __name__ == "__main__":
    unittest.main()
