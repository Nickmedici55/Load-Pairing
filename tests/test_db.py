import unittest

from loadpairing.db import Lane, Location, connect


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = connect(":memory:")

    def tearDown(self):
        self.store.close()

    def test_a_zip_is_inserted_at_the_default_dwell_the_first_time_it_is_seen(self):
        inserted = self.store.ensure_locations([Location("01040", "Holyoke", "MA")])
        self.assertEqual(inserted, 1)
        self.assertAlmostEqual(self.store.location("01040").dwell_hours, 1.0)

    def test_a_later_sheet_does_not_overwrite_a_dispatcher_override(self):
        self.store.ensure_locations([Location("01040", "Holyoke", "MA")])
        self.store.set_dwell("01040", 2.25)
        self.assertEqual(self.store.ensure_locations([Location("01040", "Holyoke", "MA")]), 0)
        self.assertAlmostEqual(self.store.location("01040").dwell_hours, 2.25)
        self.assertAlmostEqual(self.store.dwell_hours()["01040"], 2.25)

    def test_setting_dwell_on_an_unknown_zip_reports_failure(self):
        self.assertFalse(self.store.set_dwell("99999", 2.0))

    def test_coordinates_can_be_filled_in_later(self):
        self.store.ensure_locations([Location("01040")])
        self.assertIsNone(self.store.location("01040").lat)
        self.store.set_coordinates("01040", 42.2043, -72.6162)
        self.assertAlmostEqual(self.store.location("01040").lat, 42.2043)

    def test_lanes_are_cached_and_can_be_backfilled(self):
        self.store.save_lane(Lane("01020", "01040", 9.0, "estimated"))
        self.assertEqual(len(self.store.estimated_lanes()), 1)
        self.store.save_lane(Lane("01020", "01040", 9.4, "pcmiler"))
        self.assertAlmostEqual(self.store.lane("01020", "01040").miles, 9.4)
        self.assertEqual(self.store.estimated_lanes(), [])

    def test_an_unknown_lane_is_a_miss(self):
        self.assertIsNone(self.store.lane("01020", "12946"))


if __name__ == "__main__":
    unittest.main()
