import unittest

from loadpairing.arrange import ArrangeError, arrange, groups_of
from loadpairing.models import Load, Stop
from loadpairing.pairing import PairingConfig, build_trips, plan
from loadpairing.windows import WindowPolicy

DC = "01020"

#: Round-trip miles chosen so the Adirondack run is most of a shift on its own
#: and the two Chicopee-area turns are short.
MILES = {
    ("01020", "01040"): 9.0, ("01040", "01020"): 9.0,
    ("01020", "01013"): 5.0, ("01013", "01020"): 5.0,
    ("01020", "12901"): 230.0, ("12901", "12946"): 50.0, ("12946", "01020"): 250.0,
}


def miles_for(from_zip, to_zip):
    return MILES.get((from_zip, to_zip), 40.0), "test"


def load(load_id, zip_code, open_at=11.0, close_at=20.0, equipment="53LG"):
    return Load(
        load_id=load_id,
        carrier_id="PTAG",
        equipment=equipment,
        stops=(Stop("1", f"S{load_id}", zip_code, "", "", 20.0, open_at, close_at),),
    )


HOLYOKE = load("A", "01040")
CHICOPEE = load("B", "01013")
MORNING = load("C", "01040", open_at=5.0, close_at=6.0)      # a hard morning window
EARLY = load("E", "01013", open_at=5.0, close_at=5.5)        # due at the same hour, elsewhere
LONG = Load(
    load_id="D",
    carrier_id="PTAG",
    equipment="53RL",
    stops=(
        Stop("1", "S1", "12901", "Plattsburgh", "NY", 14.0, None, None),
        Stop("2", "S2", "12946", "Lake Placid", "NY", 12.0, None, None),
    ),
)


def trips_for(*loads, config=None):
    config = config or PairingConfig(dc_zip=DC, windows=WindowPolicy(enforce=True))
    return build_trips(loads, miles_for=miles_for, config=config), config


class ArrangeTest(unittest.TestCase):
    def test_putting_two_solos_on_one_driver_costs_the_combined_day(self):
        trips, config = trips_for(HOLYOKE, CHICOPEE)

        apart = arrange(trips, (("A",), ("B",)), config)
        together = arrange(trips, (("A", "B"),), config)

        self.assertEqual(apart.driver_count, 2)
        self.assertEqual(together.driver_count, 1)
        self.assertAlmostEqual(
            together.duty_hours,
            together.drivers[0].assignment.duty_hours,
            places=6,
        )
        # One driver doing both turns is longer than either alone, and shorter
        # than the two separate days added up only because the DC load is paid
        # once per turn either way.
        self.assertGreater(together.duty_hours, max(d.duty_hours for d in apart.drivers))

    def test_a_grouping_that_misses_a_delivery_time_says_so_rather_than_refusing(self):
        # Both are due within half an hour of each other in different towns:
        # no driver makes both, in either order.
        trips, config = trips_for(MORNING, EARLY)

        result = arrange(trips, (("C", "E"),), config)

        self.assertEqual(result.driver_count, 1)
        driver = result.drivers[0]
        self.assertFalse(driver.scheduled)
        self.assertEqual(len(driver.warnings), 1)
        self.assertIn("cannot reach", driver.warnings[0])
        self.assertEqual(len(result.problems), 1)

        # Apart, the same two loads are fine -- it is the pairing that fails.
        self.assertEqual(len(arrange(trips, (("C",), ("E",)), config).problems), 0)

    def test_work_over_one_shift_becomes_a_layover_with_the_reason(self):
        trips, config = trips_for(LONG, HOLYOKE, CHICOPEE, config=PairingConfig(
            dc_zip=DC, max_duty_hours=8.0, windows=WindowPolicy(enforce=False)
        ))

        result = arrange(trips, (("D", "A", "B"),), config)

        driver = result.drivers[0]
        self.assertTrue(driver.is_layover)
        self.assertIn("layover", " ".join(driver.warnings))
        self.assertGreater(driver.assignment.shifts, 1)

    def test_the_order_a_driver_runs_is_the_one_asked_for(self):
        # C only takes freight 05:00-06:00, so it has to run first. Listed the
        # other way round the day is run as listed, and fails as listed.
        trips, config = trips_for(HOLYOKE, MORNING)

        backwards = arrange(trips, (("A", "C"),), config).drivers[0]
        forwards = arrange(trips, (("C", "A"),), config).drivers[0]

        self.assertEqual(backwards.load_ids, ("A", "C"))
        self.assertFalse(backwards.scheduled)
        self.assertEqual(forwards.load_ids, ("C", "A"))
        self.assertTrue(forwards.scheduled, forwards.warnings)

    def test_an_order_that_does_not_work_is_told_the_one_that_does(self):
        trips, config = trips_for(HOLYOKE, MORNING)

        backwards = arrange(trips, (("A", "C"),), config).drivers[0]

        better = backwards.best_alternative
        self.assertIsNotNone(better)
        self.assertEqual(better.load_ids, ("C", "A"))
        self.assertGreater(better.duty_hours, 0.0)
        self.assertIn("the other way round it works", " ".join(backwards.warnings))

    def test_both_orders_of_a_pair_come_back_costed(self):
        trips, config = trips_for(HOLYOKE, CHICOPEE)

        driver = arrange(trips, (("A", "B"),), config).drivers[0]

        self.assertEqual(len(driver.options), 2)
        self.assertEqual(driver.options[0].load_ids, ("A", "B"))     # the one running
        self.assertEqual(driver.alternatives[0].load_ids, ("B", "A"))
        for option in driver.options:
            self.assertTrue(option.feasible)
            self.assertGreater(option.duty_hours, 0.0)
            self.assertGreater(option.drive_hours, 0.0)

    def test_a_solo_driver_has_one_order_and_no_alternatives(self):
        trips, config = trips_for(HOLYOKE, CHICOPEE)

        driver = arrange(trips, (("A",), ("B",)), config).drivers[0]

        self.assertEqual(len(driver.options), 1)
        self.assertEqual(driver.alternatives, ())
        self.assertIsNone(driver.best_alternative)

    def test_every_load_has_to_be_on_exactly_one_driver(self):
        trips, config = trips_for(HOLYOKE, CHICOPEE)

        with self.assertRaises(ArrangeError) as missing:
            arrange(trips, (("A",),), config)
        self.assertIn("left off every driver", str(missing.exception))

        with self.assertRaises(ArrangeError) as twice:
            arrange(trips, (("A", "B"), ("A",)), config)
        self.assertIn("two drivers at once", str(twice.exception))

        with self.assertRaises(ArrangeError) as unknown:
            arrange(trips, (("A",), ("B",), ("Z",)), config)
        self.assertIn("no such load", str(unknown.exception))

    def test_a_built_plan_arranges_back_into_itself(self):
        trips, config = trips_for(HOLYOKE, CHICOPEE, MORNING, LONG)
        built = plan(trips, config)

        same = arrange(trips, groups_of(built), config)

        self.assertEqual(same.driver_count, len(groups_of(built)))
        self.assertEqual(
            sorted(sorted(group) for group in same.groups),
            sorted(sorted(group) for group in groups_of(built)),
        )

    def test_a_plans_loads_all_survive_into_the_arrangement(self):
        trips, config = trips_for(HOLYOKE, CHICOPEE, MORNING, LONG)
        built = plan(trips, config)

        arranged = arrange(trips, groups_of(built), config)

        self.assertEqual(
            sorted(load_id for group in arranged.groups for load_id in group),
            ["A", "B", "C", "D"],
        )


if __name__ == "__main__":
    unittest.main()
