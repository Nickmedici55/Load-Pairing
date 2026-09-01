import contextlib
import io
import json
import os
import tempfile
import unittest

from loadpairing.cli import main
from tests.fixtures import dispatch_sheet, hours, write_workbook

CENTROIDS = """zip,lat,lon
01020,42.1487,-72.6079
01040,42.2043,-72.6162
01013,42.1626,-72.6076
01085,42.1362,-72.7573
02118,42.3388,-71.0726
12901,44.6995,-73.4529
12946,44.2795,-73.9860
"""

LOADS = [
    {
        "load_id": "10375774",
        "equipment": "53LG",
        "stops": [
            {"store": "Holyoke", "zip": 1040, "city": "Holyoke", "state": "MA",
             "open": hours(11.0), "close": hours(20.0), "pallets": 21},
        ],
    },
    {
        "load_id": "10375775",
        "equipment": "53RL",
        "stops": [
            {"store": "Chicopee St", "zip": 1013, "city": "Chicopee", "state": "MA",
             "open": hours(11.0), "close": hours(20.0), "pallets": 24},
        ],
    },
    {
        "load_id": "10375781",
        "equipment": "53RL",
        "stops": [
            {"store": "Plattsburgh", "zip": 12901, "city": "Plattsburgh", "state": "NY",
             "open": hours(5.25), "close": hours(9.0), "pallets": 14},
            {"store": "Lake Placid", "zip": 12946, "city": "Lake Placid", "state": "NY", "pallets": 12},
        ],
    },
    {
        "load_id": "10375790",
        "equipment": "53PLG",
        "stops": [
            {"store": "Westfield DC", "zip": 1085, "city": "Westfield", "state": "MA",
             "open": hours(0.0), "close": hours(0.0), "pallets": 28},
        ],
    },
    {"load_id": "9000", "carrier": "OTHR", "stops": [{"store": "Boston", "zip": 2118}]},
]


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sheet = os.path.join(self.dir, "NESC.xlsx")
        self.db = os.path.join(self.dir, "pairing.sqlite3")
        self.centroids = os.path.join(self.dir, "centroids.csv")
        with open(self.centroids, "w", encoding="utf-8") as handle:
            handle.write(CENTROIDS)
        write_workbook(
            self.sheet,
            {
                "Summary_1": [["title"]],
                "Reverse Order_2": [["ignored"]],
                "Dispatch Order_3": dispatch_sheet(LOADS),
            },
        )

    def plan_json(self, *extra):
        code, output = run(
            "--db", self.db, "plan", self.sheet, "--tab", "3", "--carrier", "PTAG",
            "--router", "estimated", "--centroids", self.centroids, "--json", *extra
        )
        self.assertEqual(code, 0, output)
        return json.loads(output)

    def test_sheets_lists_the_tabs(self):
        code, output = run("sheets", self.sheet)
        self.assertEqual(code, 0)
        self.assertIn("3. Dispatch Order_3", output)

    def test_plan_covers_the_carriers_loads_once_each(self):
        result = self.plan_json()
        self.assertEqual(result["loads"], 4)
        assigned = [load for a in result["assignments"] for load in a["loads"]]
        unschedulable = [load for r in result["unschedulable"] for load in r["loads"]]
        self.assertEqual(
            sorted(assigned + unschedulable),
            ["10375774", "10375775", "10375781", "10375790"],
        )
        self.assertTrue(result["estimated_mileage"])

    def test_the_carrier_defaults_to_ptag(self):
        code, output = run(
            "--db", self.db, "plan", self.sheet, "--router", "estimated",
            "--centroids", self.centroids, "--json",
        )
        self.assertEqual(code, 0, output)
        assigned = [load for a in json.loads(output)["assignments"] for load in a["loads"]]
        self.assertNotIn("9000", assigned)      # the OTHR load is left out

    def test_an_empty_carrier_reads_every_carrier_on_the_sheet(self):
        code, output = run(
            "--db", self.db, "plan", self.sheet, "--carrier", "", "--router", "estimated",
            "--centroids", self.centroids, "--json",
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["loads"], 5)

    def test_a_midnight_delivery_time_is_a_drop_and_hook_due_by_end_of_day(self):
        result = self.plan_json()
        swaps = [
            stop
            for assignment in result["assignments"]
            for stop in assignment["stops"]
            if stop["load_id"] == "10375790"
        ]
        self.assertEqual(len(swaps), 1)
        self.assertTrue(swaps[0]["drop_and_hook"])
        self.assertAlmostEqual(swaps[0]["dwell_hours"], 0.5)
        self.assertAlmostEqual(swaps[0]["delivery_time"], 23.983, places=2)
        self.assertLessEqual(swaps[0]["arrive"], swaps[0]["delivery_time"])

    def test_the_local_loads_pair_up(self):
        result = self.plan_json()
        pairs = [sorted(a["loads"]) for a in result["assignments"] if len(a["loads"]) == 2]
        self.assertTrue(pairs, result["assignments"])
        for pair in pairs:
            self.assertNotIn("10375781", pair)   # the Adirondack run is too long

    def test_every_scheduled_stop_lands_inside_its_window(self):
        result = self.plan_json()
        for assignment in result["assignments"]:
            for stop in assignment["stops"]:
                if stop["delivery_time"] is None:
                    continue
                self.assertLessEqual(stop["arrive"], stop["delivery_time"] + 1e-6, stop)
                self.assertGreaterEqual(stop["depart"], stop["window_open"], stop)

    def test_lanes_are_cached_so_a_second_run_fetches_nothing(self):
        self.plan_json()
        code, output = run("--db", self.db, "lanes", "list")
        self.assertEqual(code, 0)
        self.assertIn("01020 -> 01040", output)
        self.assertIn("estimated", output)

        code, output = run("--db", self.db, "lanes", "estimated")
        self.assertIn("backfill them with a routing source", output)

    def test_a_dwell_override_changes_the_plan(self):
        before = self.plan_json()
        code, output = run("--db", self.db, "locations", "dwell", "01040", "3.5")
        self.assertEqual(code, 0)
        self.assertIn("dwell set to 3.5 h", output)

        after = self.plan_json()
        duty_before = {frozenset(a["loads"]): a["duty_hours"] for a in before["assignments"]}
        duty_after = {frozenset(a["loads"]): a["duty_hours"] for a in after["assignments"]}
        holyoke = frozenset({"10375774", "10375775"})
        self.assertAlmostEqual(duty_after[holyoke] - duty_before[holyoke], 2.5, places=2)

    def test_locations_are_recorded_the_first_time_a_sheet_is_uploaded(self):
        self.plan_json()
        code, output = run("--db", self.db, "locations", "list")
        self.assertEqual(code, 0)
        self.assertIn("12946", output)
        self.assertIn("Lake Placid", output)
        self.assertIn(" 1.00 h", output)

    def test_ignoring_windows_is_available_and_reported(self):
        result = self.plan_json("--no-windows")
        self.assertFalse(result["config"]["enforce_windows"])

    def test_matching_equipment_can_be_required(self):
        result = self.plan_json("--match-equipment")
        self.assertTrue(result["config"]["match_equipment"])
        for assignment in result["assignments"]:
            self.assertEqual(len(set(assignment["equipment"])), 1)

    def test_text_output_names_the_drivers_and_the_estimate(self):
        code, output = run(
            "--db", self.db, "plan", self.sheet, "--carrier", "PTAG",
            "--router", "estimated", "--centroids", self.centroids, "--schedule",
        )
        self.assertEqual(code, 0)
        self.assertIn("4 loads ->", output)
        self.assertIn("estimated mileage", output)
        self.assertIn("Driver  1", output)
        self.assertIn("Lake Placid", output)

    def test_an_unknown_zip_for_a_dwell_override_is_an_error(self):
        self.plan_json()
        code, _ = run("--db", self.db, "locations", "dwell", "99999", "2.0")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
