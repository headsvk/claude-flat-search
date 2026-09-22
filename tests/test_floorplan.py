"""Floorplan OCR tests.

Split three ways, because only one third of this actually needs OCR:

  parse_area   pure text. This is the hard part and the expensive bugs live
               here, so it is tested exhaustively and needs nothing installed.
  plan_urls    pure regex over portal HTML.
  plan_score   needs PIL, and synthetic images generated here rather than
               real floorplans - portal floorplans are the portals' copyright
               and have no business in a public repo.
  end-to-end   needs the Tesseract binary; skipped when it is absent.

No image fixtures are committed. Everything visual is drawn at test time, so
the suite carries no third-party content and no binary blobs.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import floorplan

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


# ---------------------------------------------------------------------------
# parse_area - the four real cases from the module docstring, plus OCR noise.
# ---------------------------------------------------------------------------

class TestParseAreaRealPlans(unittest.TestCase):

    def test_zoopla_metric_and_imperial_on_one_line(self):
        got = floorplan.parse_area(
            "Approximate gross internal area 83.95 sqm/ 903 sq ft")
        self.assertEqual(got["sqft"], 903)
        self.assertTrue(got["confident"])

    def test_rightmove_total_beats_the_first_number(self):
        """THE case that matters. Taking the first sq ft figure gives 799,
        under the 800 floor; the real total is 814, over it. A naive parse
        silently deletes a qualifying flat."""
        got = floorplan.parse_area(
            "Approx Gross Internal Area = 74.2 sq m/799 sq ft "
            "Balcony = 1.4 sq m/ 15 sqft "
            "Total = 75.6 sq m/814 sqft")
        self.assertEqual(got["sqft"], 814)
        self.assertEqual(got["basis"], "total")
        self.assertTrue(got["confident"])

    def test_openrent_sq_dot_feet(self):
        got = floorplan.parse_area(
            "Gross Internal Area: 73 Sq. metres 791 Sq.feet")
        self.assertEqual(got["sqft"], 791)

    def test_stated_feet_beat_converting_the_metres(self):
        """The OpenRent plan says 73 sq m and 791 sq ft. 73 x 10.7639 = 785.8,
        so converting would lose 5 sq ft against a number the plan states."""
        got = floorplan.parse_area("Gross Internal Area: 73 Sq. metres 791 Sq.feet")
        self.assertEqual(got["sqft"], 791)
        self.assertNotIn("converted", got["basis"])

    def test_metric_only_is_converted(self):
        got = floorplan.parse_area("Approximate gross internal area 95.1 sq m")
        self.assertEqual(got["sqft"], round(95.1 * floorplan.SQM_TO_SQFT))
        self.assertIn("converted", got["basis"])


class TestParseAreaConfidence(unittest.TestCase):
    """`confident` false means a number was found but the plan never said it was
    the total. The caller records it and must never hard-reject on it."""

    def test_single_unlabelled_number_is_confident(self):
        got = floorplan.parse_area("1,024 sq ft")
        self.assertEqual(got["sqft"], 1024)
        self.assertTrue(got["confident"])

    def test_several_unlabelled_numbers_are_not_confident(self):
        """Could be per-room or per-floor, and the largest may still be short
        of the true total - so say so rather than guess."""
        got = floorplan.parse_area("Ground floor 520 sq ft First floor 505 sq ft")
        self.assertEqual(got["sqft"], 520)
        self.assertFalse(got["confident"])

    def test_labelled_total_is_confident_even_among_several(self):
        got = floorplan.parse_area(
            "Ground floor 520 sq ft First floor 505 sq ft Total 1025 sq ft")
        self.assertEqual(got["sqft"], 1025)
        self.assertTrue(got["confident"])

    def test_nothing_readable(self):
        got = floorplan.parse_area("kitchen bedroom bathroom hallway")
        self.assertIsNone(got["sqft"])
        self.assertFalse(got["confident"])

    def test_empty_input(self):
        self.assertIsNone(floorplan.parse_area("")["sqft"])

    def test_evidence_is_returned_for_the_chosen_number(self):
        got = floorplan.parse_area("Total = 75.6 sq m/814 sqft")
        self.assertIn("814", got["evidence"])


class TestParseAreaUnitSpellings(unittest.TestCase):
    """OCR mangles unit labels badly; the regexes are deliberately loose."""

    def test_imperial_spellings(self):
        for text in ("903 sq ft", "903 sqft", "903 Sq.feet", "903 sq. ft",
                     "903 SQ FT", "903 sqft"):
            with self.subTest(text=text):
                self.assertEqual(floorplan.parse_area(text)["sqft"], 903)

    def test_metric_spellings_all_convert(self):
        for text in ("90 sq m", "90 sqm", "90 Sq. metres", "90 sq meters",
                     "90 sq mtrs"):
            with self.subTest(text=text):
                self.assertEqual(floorplan.parse_area(text)["sqft"],
                                 round(90 * floorplan.SQM_TO_SQFT))

    def test_thousands_separator(self):
        self.assertEqual(floorplan.parse_area("1,250 sq ft")["sqft"], 1250)


class TestParseAreaImplausible(unittest.TestCase):
    """A wrong size is worse than no size: MIN_SQFT is a hard filter, so it
    deletes the listing rather than merely mislabelling it."""

    def test_balcony_below_the_floor_is_ignored(self):
        got = floorplan.parse_area("Balcony = 1.4 sq m/ 15 sqft")
        self.assertIsNone(got["sqft"])

    def test_absurdly_large_is_ignored(self):
        self.assertIsNone(floorplan.parse_area("99999 sq ft")["sqft"])

    def test_tiny_metric_is_ignored(self):
        self.assertIsNone(floorplan.parse_area("2 sq m")["sqft"])

    def test_a_year_is_not_an_area(self):
        self.assertIsNone(floorplan.parse_area("Built in 1987, refurbished 2019")["sqft"])


class TestLooksLikeAPlan(unittest.TestCase):
    def test_area_means_plan(self):
        self.assertTrue(floorplan.looks_like_a_plan({"sqft": 903}))

    def test_no_area_means_not_a_plan(self):
        """A photo of a kitchen OCRs to noise. This is the whole detector for
        OpenRent, which marks its floorplans in no way at all."""
        self.assertFalse(floorplan.looks_like_a_plan({"sqft": None}))


# ---------------------------------------------------------------------------
# plan_urls - pure regex over each portal's HTML.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Which floor the plan is of. Pure text, like parse_area.
# ---------------------------------------------------------------------------

# The OCR of OpenRent 3048266's plan, verbatim, degree signs and all - the
# image is the landlord's copyright, the text it produced is the evidence.
BURNHAM = ("Kitchen / Dining /\n\nReception Room\n22\u00b02 (6.76) max\n"
           "x 13'1 (3.99) max\n\n\\n\n11\u00b07 (3.53) max\nx 10'8 (3.25) max\n\n"
           "Bedroom\n13\u00b08 (4.17) max\nx 13\u00b04 (4.06)\n\nLower Ground Floor\n")


class TestPlanFloors(unittest.TestCase):
    """This plan states no area at all, so parse_area finds nothing and the
    whole OCR used to be discarded - including the last line, which is the one
    thing that decides the listing. It went out as a High-priority suggestion."""

    def test_the_case_that_shipped(self):
        self.assertEqual(floorplan.parse_area(BURNHAM)["sqft"], None)
        self.assertEqual(floorplan.plan_floors(BURNHAM), ["lower ground"])
        self.assertEqual(floorplan.floor_from_plan(floorplan.plan_floors(BURNHAM)),
                         {"floor_level": "lower_ground"})

    def test_degree_sign_counts_as_a_feet_mark(self):
        """Tesseract read the same image's feet marks both ways: 22\u00b02 beside
        13'1. A dimension detector that only knows the apostrophe sees half."""
        self.assertTrue(floorplan.is_plan_text(BURNHAM))

    def test_a_photograph_is_not_a_plan(self):
        for noise in ("", "Welcome home 2020", "Kitchen", "Rent PCM 3250"):
            with self.subTest(noise=noise):
                self.assertFalse(floorplan.is_plan_text(noise))

    def test_ordinal_forms(self):
        for text, want in (("Second Floor", {"floor_number": 2}),
                           ("3rd Floor", {"floor_number": 3}),
                           ("Ground Floor", {"floor_level": "ground"}),
                           ("Basement", {})):
            with self.subTest(text=text):
                self.assertEqual(
                    floorplan.floor_from_plan(floorplan.plan_floors(text)), want)

    def test_basement_label_is_lower_ground(self):
        self.assertEqual(floorplan.floor_from_plan(["basement"]),
                         {"floor_level": "lower_ground"})

    def test_lower_ground_wins_over_a_second_level(self):
        """A maisonette running down to a lower ground floor is still half below
        ground, and the description saying so has always rejected it."""
        floors = floorplan.plan_floors("Ground Floor\nLower Ground Floor")
        self.assertEqual(floors, ["ground", "lower ground"])
        self.assertEqual(floorplan.floor_from_plan(floors),
                         {"floor_level": "lower_ground"})

    def test_two_ordinals_state_nothing(self):
        """'First Floor' and 'Second Floor' on one plan give no honest answer to
        which floor the flat is on, so it stays unknown rather than guessed."""
        self.assertEqual(
            floorplan.floor_from_plan(["first", "second"]), {})

    def test_labels_are_deduplicated_in_page_order(self):
        self.assertEqual(
            floorplan.plan_floors("Ground Floor ... GROUND FLOOR ... First Floor"),
            ["ground", "first"])

    def test_bare_word_is_not_a_floor_label(self):
        """'Ground' in 'Ground Rent' is not a storey."""
        self.assertEqual(floorplan.plan_floors("Ground Rent 250"), [])


class TestPlanUrls(unittest.TestCase):

    def test_rightmove_prefers_full_size_over_thumbnail(self):
        """_max_296x197 is the thumbnail; OCR needs the full-size image."""
        html = ('"https://media.rightmove.co.uk/property-floorplan/abc_max_296x197.jpeg" '
                '"https://media.rightmove.co.uk/property-floorplan/abc_FLP_00.jpeg"')
        got = floorplan.plan_urls(html, "Rightmove")
        self.assertEqual(len(got), 1)
        self.assertIn("_FLP_00", got[0])

    def test_rightmove_falls_back_to_thumbnail_when_only_one(self):
        """A thumbnail is worse than a full plan but far better than nothing."""
        html = '"https://media.rightmove.co.uk/property-floorplan/abc_max_296x197.jpeg"'
        self.assertEqual(len(floorplan.plan_urls(html, "Rightmove")), 1)

    def test_rightmove_plan_is_not_required_to_contain_FLP(self):
        """A regex requiring FLP in the filename returned 0/20 when the real
        answer was 12/20. A zero that should be non-zero is a broken probe."""
        html = '"https://media.rightmove.co.uk/property-floorplan/1234567890abcdef.png"'
        self.assertEqual(len(floorplan.plan_urls(html, "Rightmove")), 1)

    def test_zoopla_builds_a_cdn_url_from_the_filename(self):
        html = '"floorPlan":{"filename":"abcdef0123456789.jpg"}'
        got = floorplan.plan_urls(html, "Zoopla")
        self.assertEqual(got, ["https://lid.zoocdn.com/u/2400/1800/abcdef0123456789.jpg"])

    def test_onthemarket_takes_the_large_url(self):
        html = '"floorplans":[{"largeUrl":"https://media.onthemarket.com/p/1/plan.jpg"}]'
        self.assertEqual(floorplan.plan_urls(html, "OnTheMarket"),
                         ["https://media.onthemarket.com/p/1/plan.jpg"])

    def test_openrent_names_no_plan(self):
        """OpenRent publishes floorplans as ordinary listing photos with nothing
        marking them out; they are found by appearance instead."""
        self.assertEqual(floorplan.plan_urls("<html>anything</html>", "OpenRent"), [])

    def test_duplicates_collapse(self):
        url = '"https://media.rightmove.co.uk/property-floorplan/abc_FLP_00.jpeg"'
        self.assertEqual(len(floorplan.plan_urls(url + " " + url, "Rightmove")), 1)

    def test_no_match_returns_empty(self):
        self.assertEqual(floorplan.plan_urls("<html></html>", "Rightmove"), [])


# ---------------------------------------------------------------------------
# plan_score - images generated here, never committed.
# ---------------------------------------------------------------------------

def draw_floorplan(path):
    """A line drawing on white: what every portal's floorplan looks like.
    Measured on real plans: 83-90% near-white, 100% near-grey."""
    im = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(im)
    for box in [(20, 20, 200, 160), (200, 20, 380, 160), (20, 160, 380, 280)]:
        d.rectangle(box, outline="black", width=3)
    d.text((40, 60), "BEDROOM", fill="black")
    d.text((230, 60), "RECEPTION", fill="black")
    im.save(path)
    return path


def draw_photograph(path):
    """A saturated colour photo: measured 0.1-16% near-white."""
    im = Image.new("RGB", (400, 300))
    px = im.load()
    for y in range(300):
        for x in range(400):
            px[x, y] = ((x * 7) % 200, (y * 5 + 40) % 180, (x * y) % 160)
    im.save(path)
    return path


@unittest.skipUnless(HAVE_PIL, "PIL not installed")
class TestPlanScore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_plan_scores_above_a_photograph(self):
        """The caller ranks by this score and reads the best few, rather than
        trusting a cutoff - a threshold that drifted would silently lose plans,
        and losing one costs a size on a hard requirement."""
        plan = floorplan.plan_score(draw_floorplan(self.dir / "plan.png"))
        photo = floorplan.plan_score(draw_photograph(self.dir / "photo.png"))
        self.assertGreater(plan, photo)
        self.assertGreater(plan, 0.7)
        self.assertLess(photo, 0.5)

    def test_unreadable_file_scores_zero_rather_than_raising(self):
        bad = self.dir / "not-an-image.jpg"
        bad.write_bytes(b"this is not an image")
        self.assertEqual(floorplan.plan_score(bad), 0.0)

    def test_missing_file_scores_zero_rather_than_raising(self):
        self.assertEqual(floorplan.plan_score(self.dir / "absent.jpg"), 0.0)


# ---------------------------------------------------------------------------
# End to end: a real Tesseract pass over a generated plan.
# ---------------------------------------------------------------------------

def _font():
    """A bitmap default font OCRs poorly; find a real TrueType face."""
    for name in ("arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, 28)
        except OSError:
            continue
    return None


@unittest.skipUnless(HAVE_PIL, "PIL not installed")
@unittest.skipUnless(floorplan.tesseract(), "tesseract binary not installed")
class TestOcrEndToEnd(unittest.TestCase):
    """Proves the OCR path works, not merely the parsing. Skipped wherever
    Tesseract is absent, so it never breaks someone else's checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.font = _font()
        if self.font is None:
            self.skipTest("no TrueType font available to render a test plan")

    def render(self, lines, name="plan.png"):
        im = Image.new("RGB", (900, 120 + 50 * len(lines)), "white")
        d = ImageDraw.Draw(im)
        for i, line in enumerate(lines):
            d.text((40, 40 + 50 * i), line, fill="black", font=self.font)
        path = self.dir / name
        im.save(path, dpi=(300, 300))
        return path

    def test_reads_a_simple_area_off_an_image(self):
        got = floorplan.area_from_image(self.render(["GROSS INTERNAL AREA 1024 SQ FT"]))
        self.assertEqual(got["sqft"], 1024)

    def test_reads_the_total_not_the_first_number(self):
        """The full Rightmove case, end to end through real OCR."""
        got = floorplan.area_from_image(self.render([
            "APPROX GROSS INTERNAL AREA = 74.2 SQ M / 799 SQ FT",
            "TOTAL = 75.6 SQ M / 814 SQ FT"]))
        self.assertEqual(got["sqft"], 814)
        self.assertEqual(got["basis"], "total")

    def test_an_image_with_no_area_reads_as_no_plan(self):
        got = floorplan.area_from_image(self.render(["KITCHEN", "BEDROOM"]))
        self.assertFalse(floorplan.looks_like_a_plan(got))


if __name__ == "__main__":
    unittest.main()
