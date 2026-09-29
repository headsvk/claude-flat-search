"""Postcode district -> neighbourhood name in the digest's area column."""
import unittest

from helpers import make_config  # noqa: F401  (puts src on the path)
from flatsearch import areas, render


class TestAreaNames(unittest.TestCase):
    def test_a_district_gets_its_name(self):
        cell = render.area_cell({"area": "42, 1 Water Lane NW1"})
        self.assertEqual(cell, "42, 1 Water Lane NW1 · _Camden Town / Regent's Park_")

    def test_a_sub_district_falls_back_to_its_parent(self):
        self.assertEqual(areas.area_name("EC2M"), areas.AREAS["EC2"])

    def test_a_sub_district_with_its_own_entry_uses_it(self):
        self.assertEqual(areas.area_name("SW1X"), "Belgravia / Knightsbridge")

    def test_an_address_that_already_names_the_area_is_left_alone(self):
        self.assertEqual(render.area_cell({"area": "Ruston Mews, Notting Hill W11"}),
                         "Ruston Mews, Notting Hill W11")

    def test_no_district_means_no_name(self):
        self.assertEqual(render.area_cell({"area": "4-7 Lombard Lane, London"}),
                         "4-7 Lombard Lane, London")

    def test_an_unknown_district_means_no_name(self):
        self.assertIsNone(areas.area_name("ZZ9"))


if __name__ == "__main__":
    unittest.main()
