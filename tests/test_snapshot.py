import unittest

from loadpairing.models import Load, Stop
from loadpairing.snapshot import (
    SnapshotError,
    dump_groups,
    dump_loads,
    read_groups,
    read_loads,
)

LOADS = (
    Load(
        load_id="10375781",
        carrier_id="PTAG",
        equipment="53RL",
        stops=(
            Stop("1", "08S1244", "12901", "Plattsburgh", "NY", 14.0, 5.25, 9.0),
            Stop("2", "10S1362", "12946", "Lake Placid", "NY", 12.0, None, None),
        ),
    ),
    Load("10375790", "PTAG", "53PLG", (Stop("1", "08S0648", "01085", "Westfield", "MA", 28.0, 0.0, 0.0),)),
)


class SnapshotTest(unittest.TestCase):
    def test_loads_survive_the_round_trip_intact(self):
        self.assertEqual(read_loads(dump_loads(LOADS)), LOADS)

    def test_a_stop_with_no_window_stays_without_one(self):
        restored = read_loads(dump_loads(LOADS))
        self.assertIsNone(restored[0].stops[1].window_open)
        self.assertFalse(restored[0].stops[1].has_window)

    def test_a_drop_and_hook_is_still_one_after_the_round_trip(self):
        restored = read_loads(dump_loads(LOADS))
        self.assertTrue(restored[1].stops[0].is_drop_and_hook)

    def test_groups_survive_the_round_trip(self):
        groups = (("10375781",), ("10375790", "10375774"))
        self.assertEqual(read_groups(dump_groups(groups)), groups)

    def test_a_submission_that_is_not_a_plan_is_refused_not_crashed_on(self):
        for text in ("", "not json", '{"loads": []}', "[1, 2]"):
            with self.assertRaises(SnapshotError):
                read_loads(text)

    def test_a_load_with_no_stops_is_refused(self):
        with self.assertRaises(SnapshotError) as caught:
            read_loads('[{"load_id": "A", "stops": []}]')
        self.assertIn("no delivery stops", str(caught.exception))

    def test_a_stop_with_no_zip_is_refused(self):
        with self.assertRaises(SnapshotError) as caught:
            read_loads('[{"load_id": "A", "stops": [{"store": "S"}]}]')
        self.assertIn("ZIP", str(caught.exception))

    def test_a_window_that_is_not_a_number_is_refused(self):
        with self.assertRaises(SnapshotError) as caught:
            read_loads('[{"load_id":"A","stops":[{"zip":"01040","window_open":"soon"}]}]')
        self.assertIn("window open", str(caught.exception))

    def test_a_grouping_that_is_not_a_list_of_lists_is_refused(self):
        with self.assertRaises(SnapshotError):
            read_groups('["10375781"]')


if __name__ == "__main__":
    unittest.main()
