"""A re-run after one portal failed searches only that portal.

2026-11-05: Zoopla was challenged, the search refused to commit, and the one
re-run the runbook allows paged Rightmove, OnTheMarket and OpenRent again -
655 listings, half an hour - to get them back unchanged. Each portal that
comes back clean is now saved as it finishes and reused for the rest of the
morning. What these pin down is the part that must not go wrong: a portal
that FAILED is never reused, and nothing survives a commit or a new day.
"""
import asyncio
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import core, fetch  # noqa: E402
from helpers import make_config  # noqa: E402

GOOD = "https://good.example/search?x=1"
BAD = "https://bad.example/search?x=1"


class TestSearchReuse(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(self.tmp.name, searches=(GOOD, BAD))
        self.out = self.cfg.runs_dir / "stage1.json"
        self.calls = []
        self.failing = {"bad.example"}

        async def collect(url, seen, known=None, incremental=False):
            host = url.split("/")[2]
            self.calls.append(host)
            if host in self.failing:
                return [], "empty first page"
            return [{"url": "https://%s/1" % host, "platform": host}], "ok"

        async def preflight(cfg=None):
            return None

        for name, fn in {"collect": collect, "preflight": preflight}.items():
            real = getattr(fetch, name)
            setattr(fetch, name, fn)
            self.addCleanup(setattr, fetch, name, real)
        real_uniform = fetch.random.uniform
        fetch.random.uniform = lambda a, b: 0
        self.addCleanup(setattr, fetch.random, "uniform", real_uniform)

    def search(self, **kw):
        self.calls.clear()
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            ok = asyncio.run(fetch.run_search(self.cfg, self.out, **kw))
        self.printed = buf.getvalue()
        return ok

    def listed(self):
        return sorted(l["url"] for l in core.read_json(self.out)["listings"])

    def test_rerun_searches_only_the_failed_portal(self):
        self.assertFalse(self.search())
        self.assertEqual(sorted(self.calls), ["bad.example", "good.example"])

        self.failing.clear()
        self.assertTrue(self.search())
        self.assertEqual(self.calls, ["bad.example"])
        self.assertIn("reused good.example", self.printed)
        # The reused portal's listings are in the stage file, not just the
        # re-searched one's - the stage file is all commit ever reads.
        self.assertEqual(self.listed(), ["https://bad.example/1",
                                         "https://good.example/1"])

    def test_a_failed_portal_is_never_reused(self):
        self.search()
        self.search()
        self.assertIn("bad.example", self.calls)
        self.assertNotIn("good.example", self.calls)

    def test_a_portal_that_was_clean_then_failed_is_searched_again(self):
        self.failing.clear()
        self.search(fresh=True)
        self.failing.add("good.example")
        self.assertFalse(self.search(fresh=True))
        self.failing.clear()
        self.search()
        self.assertIn("good.example", self.calls)

    def test_fresh_searches_everything(self):
        self.search()
        self.failing.clear()
        self.search(fresh=True)
        self.assertEqual(sorted(self.calls), ["bad.example", "good.example"])

    def test_not_reused_on_another_day(self):
        self.search()
        path = self.cfg.runs_dir / core.SEARCH_REUSE
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["portals"]["good.example"]["key"]["date"] = "2000-01-01"
        path.write_text(json.dumps(saved), encoding="utf-8")
        self.search()
        self.assertIn("good.example", self.calls)

    def test_not_reused_when_the_search_urls_change(self):
        self.search()
        self.cfg = make_config(self.tmp.name,
                               searches=(GOOD + "&y=2", BAD))
        self.search()
        self.assertIn("good.example", self.calls)

    def test_not_reused_across_incremental(self):
        self.search()
        real = core.known_urls
        core.known_urls = lambda cfg: set()
        self.addCleanup(setattr, core, "known_urls", real)
        self.search(incremental=True)
        self.assertIn("good.example", self.calls)

    def test_commit_discards_the_saved_portals(self):
        self.failing.clear()
        self.assertTrue(self.search())
        self.assertTrue((self.cfg.runs_dir / core.SEARCH_REUSE).exists())
        with contextlib.redirect_stdout(io.StringIO()):
            core.commit(self.cfg, self.out)
        self.assertFalse((self.cfg.runs_dir / core.SEARCH_REUSE).exists())
        self.search()
        self.assertEqual(sorted(self.calls), ["bad.example", "good.example"])


if __name__ == "__main__":
    unittest.main()
