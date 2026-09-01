import os
import tempfile
import unittest

from loadpairing.xlsx import SerialDateTime, XlsxError, read_sheet, sheet_names
from tests.fixtures import Time, hours, write_workbook


class XlsxReaderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "book.xlsx")

    def test_lists_tabs_in_order(self):
        write_workbook(self.path, {"Summary_1": [["a"]], "Reverse Order_2": [["b"]], "Dispatch Order_3": [["c"]]})
        self.assertEqual(sheet_names(self.path), ["Summary_1", "Reverse Order_2", "Dispatch Order_3"])

    def test_reads_tab_by_number_and_by_name(self):
        write_workbook(self.path, {"one": [["a"]], "two": [["b"]], "three": [["c"]]})
        self.assertEqual(read_sheet(self.path, 3).rows, [["c"]])
        self.assertEqual(read_sheet(self.path, "two").rows, [["b"]])

    def test_rejects_a_tab_out_of_range(self):
        write_workbook(self.path, {"one": [["a"]]})
        with self.assertRaises(XlsxError):
            read_sheet(self.path, 4)

    def test_keeps_column_positions_when_cells_are_skipped(self):
        write_workbook(self.path, {"one": [["a", "", "", "d"]]})
        self.assertEqual(read_sheet(self.path, 1).rows, [["a", None, None, "d"]])

    def test_time_cells_come_back_tagged(self):
        write_workbook(self.path, {"one": [[hours(5.25), 5.25]]})
        first, second = read_sheet(self.path, 1).rows[0]
        self.assertIsInstance(first, SerialDateTime)
        self.assertAlmostEqual(first.hours_past_midnight, 5.25)
        self.assertNotIsInstance(second, SerialDateTime)

    def test_numbers_and_strings_keep_their_types(self):
        write_workbook(self.path, {"one": [[1013, "PTAG", 24.5]]})
        self.assertEqual(read_sheet(self.path, 1).rows[0], [1013.0, "PTAG", 24.5])


if __name__ == "__main__":
    unittest.main()
