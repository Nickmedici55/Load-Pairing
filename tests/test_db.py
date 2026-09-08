import sqlite3
import unittest

from loadpairing.db import Lane, Location, ServiceCenter, SqliteStore, connect


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
        self.assertAlmostEqual(self.store.dwell_hours("01020")["01040"], 2.25)

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

    def test_two_service_centers_delivering_to_one_zip_keep_separate_rows(self):
        self.store.save_service_center(ServiceCenter("06103", "Hartford SC", "Hartford", "CT"))
        self.store.ensure_locations(
            [
                Location("01040", "Holyoke", "MA", store="1234", service_center="01020"),
                Location("01040", "Holyoke", "MA", store="7788", service_center="06103"),
            ]
        )

        self.store.set_dwell("01040", 3.0, "01020")

        self.assertAlmostEqual(self.store.location("01040", "01020").dwell_hours, 3.0)
        self.assertAlmostEqual(self.store.location("01040", "06103").dwell_hours, 1.0)
        self.assertEqual(self.store.location("01040", "01020").store, "1234")
        self.assertEqual(self.store.location("01040", "06103").store, "7788")
        self.assertAlmostEqual(self.store.dwell_hours("01020")["01040"], 3.0)
        self.assertAlmostEqual(self.store.dwell_hours("06103")["01040"], 1.0)

    def test_locations_can_be_listed_for_one_service_center(self):
        self.store.save_service_center(ServiceCenter("06103", "Hartford SC", "Hartford", "CT"))
        self.store.ensure_locations([Location("01040", service_center="01020")])
        self.store.ensure_locations([Location("06010", service_center="06103")])

        self.assertEqual([loc.zip for loc in self.store.locations("01020")], ["01040"])
        self.assertEqual([loc.zip for loc in self.store.locations("06103")], ["06010"])
        self.assertEqual(len(self.store.locations()), 2)

    def test_coordinates_are_a_fact_about_the_zip_not_the_service_center(self):
        self.store.save_service_center(ServiceCenter("06103", "Hartford SC", "Hartford", "CT"))
        self.store.ensure_locations(
            [
                Location("01040", service_center="01020"),
                Location("01040", service_center="06103"),
            ]
        )
        self.store.set_coordinates("01040", 42.2043, -72.6162)
        self.assertAlmostEqual(self.store.location("01040", "06103").lat, 42.2043)

    def test_an_empty_database_is_seeded_with_a_service_center(self):
        self.assertEqual([sc.zip for sc in self.store.service_centers()], ["01020"])

    def test_a_service_center_is_added_once_and_renamed_after_that(self):
        self.assertTrue(self.store.save_service_center(ServiceCenter("06103", "Hartford", "Hartford", "CT")))
        self.store.ensure_locations([Location("06010", service_center="06103")])

        self.assertFalse(self.store.save_service_center(ServiceCenter("06103", "Hartford SC", "Hartford", "CT")))
        self.assertEqual(self.store.service_center("06103").name, "Hartford SC")
        self.assertEqual(len(self.store.locations("06103")), 1)

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

    def test_locations_predating_service_centers_are_adopted_by_the_default(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE location (zip TEXT PRIMARY KEY, city TEXT, state TEXT, "
            "lat REAL, lon REAL, dwell_hours REAL NOT NULL DEFAULT 1.0, "
            "first_seen TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        connection.execute(
            "INSERT INTO location (zip, city, state, dwell_hours) "
            "VALUES ('12946', 'Lake Placid', 'NY', 2.5)"
        )
        connection.commit()

        store = SqliteStore(connection)
        store.create_schema()

        adopted = store.location("12946", "01020")
        self.assertIsNotNone(adopted)
        self.assertAlmostEqual(adopted.dwell_hours, 2.5)     # the override survives
        self.assertEqual(adopted.city, "Lake Placid")
        self.assertEqual([sc.zip for sc in store.service_centers()], ["01020"])
        store.close()


if __name__ == "__main__":
    unittest.main()
