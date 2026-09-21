"""Reading the availability date off a listing that states one.

docs/portals.md recorded Zoopla and OnTheMarket as publishing no availability
date, so neither extractor looked, and 0 of 93 Zoopla and 0 of 74 OnTheMarket
records carried one. Measured against 175 cached pages on 2026-09-21 the claim
was wrong in the way that matters: neither portal puts it in a FIELD, but both
print it - OnTheMarket labelled inside "Letting details" (64 of 79 pages),
Zoopla in the agent's prose (32 of 96). OpenRent was worse than missing: its
own regex captured a 20-character window of prose, so 106 records held values
like "to move in" and "Today" that parse_date read as nothing at all.

Every string below is copied from a real cached page.
"""
import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import portals  # noqa: E402

TODAY = dt.date(2026, 9, 21)


class TestShapesSeenInTheWild(unittest.TestCase):

    def check(self, text, expected):
        self.assertEqual(portals.availability(text, today=TODAY), expected, text)

    def test_the_labelled_ones(self):
        self.check("Letting details: Availability date: 23 Sep 2026 Furnished",
                   "2026-09-23")
        self.check("Availability date: 1 Nov 2026", "2026-11-01")
        self.check("Let available date: 26/09/2026", "2026-09-26")

    def test_the_prose_ones(self):
        self.check("Available to move in from 15 October 2026 - Maximum num",
                   "2026-10-15")
        self.check("Available from 8 November 2026", "2026-11-08")

    def test_a_comma_before_the_year(self):
        self.check("Property comes furnished - Available from 28 October, 2026 -",
                   "2026-10-28")

    def test_a_month_with_no_day(self):
        """The month is the claim; the alternative is throwing away the only
        date on the page."""
        self.check("Available to move in from November 2026, this property",
                   "2026-11-01")

    def test_an_ordinal_with_no_year_reads_as_the_next_one(self):
        """Agents write this about the coming weeks, not about last year."""
        self.check("at Embassy Gardens, available from 27th October", "2026-10-27")

    def test_a_date_just_past_is_still_this_year(self):
        """Within a month behind is a listing that came free recently, not one
        twelve months out."""
        self.check("Available from 1st September", "2026-09-01")

    def test_now_and_immediately_are_answers_not_silence(self):
        self.check("Letting details: Available now Unfurnished Deposit", "2026-09-21")
        self.check("Available to move in from now onwards", "2026-09-21")
        self.check("Key features: Students Only / Available Immediately / 675 PCM",
                   "2026-09-21")
        self.check("Let available date: Now", "2026-09-21")


class TestWhatMustNotBecomeADate(unittest.TestCase):
    """Only a value that PARSES is returned. Listing prose is full of the word,
    and a wrong date outranks a missing one - it looks like a reading."""

    def none(self, text):
        self.assertIsNone(portals.availability(text, today=TODAY), text)

    def test_parking_available_at_extra_cost(self):
        self.none("Underground parking available (at extra cost).")

    def test_phone_bookings_available_9am_9pm(self):
        self.none("Request Details form responded to 24/7, with phone bookings "
                  "available 9am-9pm, 7 days a week.")

    def test_ask_agent_is_not_a_date(self):
        self.none("Let available date: Ask agent")
        self.assertIsNone(portals.availability_value("Ask agent", today=TODAY))

    def test_a_bare_label_with_nothing_after_it(self):
        self.none("Availability date:")


class TestTheExtractorsUseIt(unittest.TestCase):

    def test_zoopla_reads_its_prose(self):
        text = ("About this property Two bedrooms Lift Concierge "
                "Property description Available to move in from 14 November 2026, "
                "this property benefits from a communal garden and a porter, and "
                "is offered unfurnished to a long let tenant in the building.")
        _, info = portals.zoopla_detail("", text)
        self.assertEqual(info.get("available_from"), "2026-11-14")

    def test_onthemarket_prefers_its_label(self):
        """The labelled value is the portal speaking; the description is an
        agent writing about something else."""
        text = ("Letting details Availability date: 30 Oct 2026 Deposit: 4038 "
                "Long term let Features and description A flat that was "
                "available immediately when it was first advertised.")
        _, info = portals.otm_detail("", text)
        self.assertEqual(info.get("available_from"), "2026-10-30")

    def test_openrent_no_longer_stores_prose(self):
        text = ("2 tenants max. 2 bathrooms A bright flat. Available to move in "
                "from 08 November 2026 - Maximum number of tenants is 4 "
                "Price & Bills Rent PCM 3900")
        _, info = portals.openrent_detail("", text)
        self.assertEqual(info.get("available_from"), "2026-11-08")


class TestTheFactContainersAreReadFirst(unittest.TestCase):
    """Both portals put the date in a fact label, not a typed field.

    Measured on live pages, 2026-09-21. Neither has an `availableFrom` key of
    any kind - "Available from" is a VALUE, which is why searching the payload
    for an availability KEY finds nothing:

        Zoopla        tagsV2: [{label: "Available from 8 November 2026"}, ...]
        OnTheMarket   lettingDetails: {items: ["Availability date: 1 Nov 2026",
                                               "Furnished"]}

    Reading these instead of the page text is what makes the parser safe from
    the advert's own prose.
    """

    ZOOPLA = (r'{\"tagsV2\":[{\"label\":\"Available from 8 November 2026\"},'
              r'{\"label\":\"Unfurnished\"}]}')
    OTM = ('{"lettingDetails":{"items":["Availability date: 1 Nov 2026",'
           '"Furnished"]}}')

    def test_zoopla_tags_are_read_out_of_the_escaped_flight_payload(self):
        self.assertEqual(
            portals.fact_labels(self.ZOOPLA, portals.ZOOPLA_TAGS_RE,
                                portals.ZOOPLA_TAG_LABEL_RE),
            ["Available from 8 November 2026", "Unfurnished"])

    def test_onthemarket_items_are_read_out_of_next_data(self):
        self.assertEqual(
            portals.fact_labels(self.OTM, portals.OTM_LETTING_RE,
                                portals.OTM_ITEM_RE),
            ["Availability date: 1 Nov 2026", "Furnished"])

    def test_the_tag_beats_the_advert_on_furnishing(self):
        """The page text says "offered part unfurnished" in the agent's
        prose while the portal's own tag says Unfurnished. The tag is the
        portal speaking. Until 2026-09-21 OnTheMarket furnishing was read by a
        regex holding a literal backspace byte where a word boundary was
        meant - a heredoc had eaten the backslash - so it matched nothing and
        0 of 74 OnTheMarket listings carried a furnishing at all."""
        _, info = portals.zoopla_detail(
            self.ZOOPLA, "The Property is offered part unfurnished, with a kitchen.")
        self.assertEqual(info.get("furnished"), "Unfurnished")

    def test_onthemarket_furnishing_comes_off_the_items(self):
        _, info = portals.otm_detail(self.OTM, "")
        self.assertEqual(info.get("furnished"), "Furnished")

    def test_the_date_comes_off_the_labels(self):
        _, zinfo = portals.zoopla_detail(self.ZOOPLA, "")
        _, oinfo = portals.otm_detail(self.OTM, "")
        self.assertEqual(zinfo.get("available_from"), "2026-11-08")
        self.assertEqual(oinfo.get("available_from"), "2026-11-01")

    def test_the_page_text_is_still_the_fallback(self):
        """A payload can change shape overnight, and an extractor that
        silently returns nothing is this project's characteristic bug."""
        _, info = portals.zoopla_detail(
            "<html>no payload here</html>",
            "Available to move in from 14 November 2026, this property benefits")
        self.assertEqual(info.get("available_from"), "2026-11-14")


if __name__ == "__main__":
    unittest.main()
