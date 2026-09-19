"""Dependency preflight tests.

`fetch.py` refuses to start a run it cannot finish. The distinction these tests
pin down is the one that matters:

  REQUIRED missing (playwright, chromium)  -> abort, naming the fix command.
  OPTIONAL missing (Tesseract, Pillow)     -> continue, but SAY SO loudly.

The second case is the dangerous one and the reason this exists. Without
Tesseract, floorplan OCR silently reads nothing, every listing is filed "size
not stated", and the run looks perfectly healthy - the same shape as the silent
extraction failures that have cost this project the most time.

Nothing here touches the network; the browser launch is stubbed out.
"""
import asyncio
import builtins
import contextlib
import io
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import fetch
from flatsearch import floorplan
from helpers import make_config


class _StubPlaywright:
    """A chromium that launches and closes cleanly."""
    class _Browser:
        async def close(self):
            return None

    class _Chromium:
        async def launch(self, **kw):
            return _StubPlaywright._Browser()

    def __init__(self):
        self.chromium = self._Chromium()

    async def start(self):
        return self

    async def stop(self):
        return None


class _BrokenPlaywright:
    """`pip install playwright` without `playwright install chromium`: imports
    fine, then fails on the first launch a third of the way into a run."""
    async def start(self):
        raise RuntimeError("Executable doesn't exist at ...\\chrome-win\\chrome.exe")


class PreflightTestCase(unittest.TestCase):

    def setUp(self):
        import playwright.async_api as pa
        self.pa = pa
        self._real_factory = pa.async_playwright
        self._real_tesseract = floorplan.tesseract
        self._real_import = builtins.__import__
        pa.async_playwright = lambda: _StubPlaywright()
        self.addCleanup(self._restore)

    def _restore(self):
        self.pa.async_playwright = self._real_factory
        floorplan.tesseract = self._real_tesseract
        builtins.__import__ = self._real_import

    def run_preflight(self):
        """-> (exit_message or None, stdout)."""
        buf = io.StringIO()
        message = None
        try:
            with contextlib.redirect_stdout(buf):
                asyncio.run(fetch.preflight())
        except SystemExit as exc:
            message = str(exc.code)
        return message, buf.getvalue()


class TestHealthyEnvironment(PreflightTestCase):

    def test_does_not_abort(self):
        floorplan.tesseract = lambda: r"C:\fake\tesseract.exe"
        message, _ = self.run_preflight()
        self.assertIsNone(message)

    def test_is_quiet(self):
        """No warning when nothing is wrong - otherwise the warnings that do
        matter get tuned out."""
        floorplan.tesseract = lambda: r"C:\fake\tesseract.exe"
        _, out = self.run_preflight()
        self.assertNotIn("!", out)


class TestOptionalDependencies(PreflightTestCase):
    """Degrade, but never in silence."""

    def test_missing_tesseract_does_not_abort(self):
        floorplan.tesseract = lambda: None
        message, _ = self.run_preflight()
        self.assertIsNone(message)

    def test_missing_tesseract_warns(self):
        floorplan.tesseract = lambda: None
        _, out = self.run_preflight()
        self.assertIn("OCR DISABLED", out)

    def test_the_warning_says_sizes_will_not_be_read(self):
        """'Tesseract missing' means nothing to someone reading a digest; 'no
        listing will be sized' is the consequence they need to know."""
        floorplan.tesseract = lambda: None
        _, out = self.run_preflight()
        self.assertIn("NOT", out)
        self.assertIn("size not stated", out)


class TestRequiredDependencies(PreflightTestCase):

    def test_missing_playwright_aborts(self):
        real = self._real_import

        def no_playwright(name, *a, **k):
            if name.startswith("playwright"):
                raise ImportError("No module named 'playwright'")
            return real(name, *a, **k)

        builtins.__import__ = no_playwright
        message, _ = self.run_preflight()
        self.assertIsNotNone(message)

    def test_missing_playwright_names_the_fix(self):
        real = self._real_import

        def no_playwright(name, *a, **k):
            if name.startswith("playwright"):
                raise ImportError("No module named 'playwright'")
            return real(name, *a, **k)

        builtins.__import__ = no_playwright
        message, _ = self.run_preflight()
        self.assertIn("playwright install chromium", message)

    def test_uninstalled_chromium_aborts(self):
        self.pa.async_playwright = lambda: _BrokenPlaywright()
        message, _ = self.run_preflight()
        self.assertIsNotNone(message)

    def test_uninstalled_chromium_is_reported_as_a_launch_failure(self):
        """Distinct from the import failure: the module is there, the browser
        binary is not, and the fix is a different command."""
        self.pa.async_playwright = lambda: _BrokenPlaywright()
        message, _ = self.run_preflight()
        self.assertIn("will not launch", message)
        self.assertIn("playwright install chromium", message)



class TestConfigUrlDrift(unittest.TestCase):
    """Thresholds live in two places - the config, and the query params baked
    into each search URL - and nothing keeps them in step.

    Only the TIGHTER direction is reported, because only it loses listings: a
    URL tighter than the config never fetches them, so they cannot appear in
    any digest and the morning simply looks quiet.
    """

    RM = ("https://www.rightmove.co.uk/property-to-rent/find.html?"
          "minBedrooms=2&minBathrooms=2&maxPrice=%d")

    def cfg(self, urls, **over):
        if isinstance(urls, str):
            urls = [urls]
        return make_config(searches=tuple(urls), **over)

    def test_matching_url_and_config_is_silent(self):
        cfg = self.cfg(self.RM % 3000)
        self.assertEqual(fetch.config_url_drift(cfg), [])

    def test_url_cheaper_than_budget_is_reported(self):
        """Raising the budget without editing the URLs silently deletes the
        whole band that was just opened up."""
        out = fetch.config_url_drift(self.cfg(self.RM % 3000, budget_pcm=5000))
        self.assertEqual(len(out), 1)
        self.assertIn("BELOW", out[0])
        self.assertIn("3000", out[0])
        self.assertIn("5000", out[0])

    def test_url_dearer_than_budget_is_silent(self):
        """Merely wasteful: they are fetched, then rejected locally."""
        self.assertEqual(
            fetch.config_url_drift(self.cfg(self.RM % 4000, budget_pcm=3000)), [])

    def test_url_demanding_more_bathrooms_is_reported(self):
        out = fetch.config_url_drift(self.cfg(self.RM % 3000, min_bathrooms=1))
        self.assertTrue(any("minBathrooms" in o and "ABOVE" in o for o in out))

    def test_url_demanding_fewer_bedrooms_is_silent(self):
        out = fetch.config_url_drift(self.cfg(self.RM % 3000, min_bedrooms=3))
        self.assertFalse(any("minBedrooms" in o for o in out))

    def test_each_portal_spelling_is_recognised(self):
        for param in ("maxPrice", "price_max", "max-price", "prices_max"):
            with self.subTest(param=param):
                url = "https://www.zoopla.co.uk/to-rent/?%s=2000" % param
                out = fetch.config_url_drift(self.cfg(url, budget_pcm=3000))
                self.assertEqual(len(out), 1, "%s not recognised" % param)

    def test_identical_urls_do_not_repeat_the_same_warning(self):
        out = fetch.config_url_drift(
            self.cfg([self.RM % 3000, (self.RM % 3000) + "&foo=1"], budget_pcm=5000))
        self.assertEqual(len(out), 1)

    def test_a_missing_param_is_not_an_error(self):
        """OnTheMarket deliberately carries no bathroom param - it is ignored
        there, so its filtering happens locally."""
        url = "https://www.onthemarket.com/to-rent/?max-price=3000&min-bedrooms=2"
        self.assertEqual(fetch.config_url_drift(self.cfg(url)), [])


if __name__ == "__main__":
    unittest.main()
