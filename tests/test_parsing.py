import os
import tempfile
import unittest

from loadpairing.parsing import ParseError, normalize_zip, parse_time, parse_workbook
from tests.fixtures import dispatch_sheet, hours, write_workbook


def build(loads, path, tab_name="Dispatch Order_3"):
    write_workbook(
        path,
        {"Summary_1": [["x"]], "Reverse Order_2": [["y"]], tab_name: dispatch_sheet(loads)},
    )
    return path


class ParseTimeTest(unittest.TestCase):
    def test_reads_the_shapes_a_window_cell_arrives_in(self):
        cases = {
            "05:15": 5.25,
            "5:15": 5.25,
            "5:15 PM": 17.25,
            "0515": 5.25,
            "00:00": 0.0,
            "12:00 AM": 0.0,
            "12:00 PM": 12.0,
            0.21875: 5.25,        # a bare fraction of a day
        }
        for value, expected in cases.items():
            self.assertAlmostEqual(parse_time(value), expected, msg=repr(value))

    def test_empty_cells_are_no_window(self):
        for value in (None, "", "   ", "n/a"):
            self.assertIsNone(parse_time(value))


class NormalizeZipTest(unittest.TestCase):
    def test_pads_floats_back_to_five_digits(self):
        self.assertEqual(normalize_zip(1013.0), "01013")
        self.assertEqual(normalize_zip("1013"), "01013")
        self.assertEqual(normalize_zip(12946), "12946")

    def test_drops_the_plus_four(self):
        self.assertEqual(normalize_zip("01013-1234"), "01013")

    def test_blank_stays_blank(self):
        self.assertEqual(normalize_zip(None), "")
        self.assertEqual(normalize_zip(""), "")


class ParseWorkbookTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "dispatch.xlsx")
        self.loads = [
            {
                "load_id": "10375774",
                "equipment": "53LG",
                "stops": [
                    {
                        "store": "Holyoke",
                        "zip": 1040,
                        "city": "Holyoke",
                        "state": "MA",
                        "open": hours(11.0),
                        "close": hours(20.0),
                        "pallets": 21,
                    }
                ],
            },
            {
                "load_id": "10375781",
                "equipment": "53RL",
                "stops": [
                    {"store": "Plattsburgh", "zip": 12901, "city": "Plattsburgh", "state": "NY",
                     "open": hours(5.25), "close": hours(9.0), "pallets": 10},
                    {"store": "Massena", "zip": 13662, "city": "Massena", "state": "NY", "pallets": 8},
                    {"store": "Lake Placid", "zip": 12946, "city": "Lake Placid", "state": "NY", "pallets": 6},
                ],
            },
            {"load_id": "9000", "carrier": "OTHR", "stops": [{"store": "Boston", "zip": 2118}]},
        ]
        build(self.loads, self.path)

    def test_reads_every_load_block_past_the_repeated_header(self):
        parsed = parse_workbook(self.path, tab=3)
        self.assertEqual([load.load_id for load in parsed.loads], ["10375774", "10375781", "9000"])

    def test_rows_with_a_blank_carrier_are_more_stops_on_the_same_load(self):
        parsed = parse_workbook(self.path, tab=3, carrier_id="PTAG")
        second = parsed.loads[1]
        self.assertEqual([stop.store for stop in second.stops], ["Plattsburgh", "Massena", "Lake Placid"])
        self.assertEqual(second.zips, ("12901", "13662", "12946"))
        self.assertEqual(second.pallets, 24)

    def test_carrier_filter(self):
        parsed = parse_workbook(self.path, tab=3, carrier_id="PTAG")
        self.assertEqual([load.load_id for load in parsed.loads], ["10375774", "10375781"])
        self.assertEqual(parsed.stop_count, 4)

    def test_equipment_and_windows_come_from_the_block_header_row(self):
        parsed = parse_workbook(self.path, tab=3, carrier_id="PTAG")
        first = parsed.loads[0].stops[0]
        self.assertEqual(parsed.loads[0].equipment, "53LG")
        self.assertAlmostEqual(first.window_open, 11.0)
        self.assertAlmostEqual(first.window_close, 20.0)
        self.assertIsNone(parsed.loads[1].stops[1].window_open)

    def test_a_missing_header_is_an_error(self):
        path = os.path.join(tempfile.mkdtemp(), "bad.xlsx")
        write_workbook(path, {"one": [["nothing", "useful"]] * 10})
        with self.assertRaises(ParseError):
            parse_workbook(path, tab=1)


if __name__ == "__main__":
    unittest.main()
