import sqlite3
import unittest

from loadpairing.db import Lane, Location, SqliteStore, connect


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

    def test_the_store_number_behind_a_zip_is_recorded(self):
        self.store.ensure_locations([Location("01040", "Holyoke", "MA", store="1234")])
        self.assertEqual(self.store.location("01040").store, "1234")

    def test_a_second_store_at_one_zip_is_added_rather_than_replacing_the_first(self):
        self.store.ensure_locations(
            [
                Location("01040", "Holyoke", "MA", store="1234"),
                Location("01040", "Holyoke", "MA", store="5678"),
            ]
        )
        self.assertEqual(self.store.location("01040").store, "1234, 5678")

        self.store.ensure_locations([Location("01040", "Holyoke", "MA", store="9012")])
        self.assertEqual(self.store.location("01040").store, "1234, 5678, 9012")

    def test_a_store_already_on_file_is_not_recorded_twice(self):
        self.store.ensure_locations([Location("01040", "Holyoke", "MA", store="1234")])
        self.store.ensure_locations([Location("01040", "Holyoke", "MA", store="1234")])
        self.assertEqual(self.store.location("01040").store, "1234")

    def test_the_dc_has_no_store_behind_it(self):
        self.store.ensure_locations([Location("01020")])
        self.assertEqual(self.store.location("01020").store, "")

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


class MigrationTest(unittest.TestCase):
    def test_a_database_predating_the_store_column_gains_it(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE location (zip TEXT PRIMARY KEY, city TEXT, state TEXT, "
            "lat REAL, lon REAL, dwell_hours REAL NOT NULL DEFAULT 1.0, "
            "first_seen TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        connection.execute("INSERT INTO location (zip, city, state) VALUES ('01040', 'Holyoke', 'MA')")
        connection.commit()

        store = SqliteStore(connection)
        store.create_schema()

        self.assertEqual(store.location("01040").store, "")
        store.ensure_locations([Location("01040", "Holyoke", "MA", store="1234")])
        self.assertEqual(store.location("01040").store, "1234")
        store.close()


if __name__ == "__main__":
    unittest.main()
