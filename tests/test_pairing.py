import unittest

from loadpairing.costing import cost_trip
from loadpairing.models import Load, Stop
from loadpairing.pairing import (
    Candidate,
    PairingConfig,
    build_trips,
    delivery_order,
    evaluate_pair,
    plan,
)
from loadpairing.windows import WindowPolicy

DC = "01020"


def make_trip(load_id, one_way_miles, equipment="53LG", windows=(None, None), dwell=1.0, stops=1):
    """A load whose stops all sit ``one_way_miles`` from the DC, in a line."""
    distance = {}
    stop_list = []
    for index in range(stops):
        zip_code = f"9{load_id[-1]}{index:03d}"
        distance[(DC, zip_code)] = one_way_miles
        distance[(zip_code, DC)] = one_way_miles
        stop_list.append(
            Stop(
                order=str(index + 1),
                store=f"{load_id}-{index + 1}",
                zip=zip_code,
                window_open=windows[0] if index == 0 else None,
                window_close=windows[1] if index == 0 else None,
            )
        )
    load = Load(load_id=load_id, carrier_id="PTAG", equipment=equipment, stops=tuple(stop_list))
    return cost_trip(
        load,
        DC,
        lambda a, b: (distance.get((a, b), 0.0), "estimated"),
        lambda _z: dwell,
    )


NO_WINDOWS = PairingConfig(dc_zip=DC, windows=WindowPolicy(enforce=False))


def _max_pairs(result):
    """Largest number of disjoint pairs available, found by exhaustion."""
    pairs = [set(candidate.load_ids) for candidate in result.candidates]

    def best(index, used):
        if index == len(pairs):
            return 0
        skipped = best(index + 1, used)
        if pairs[index] & used:
            return skipped
        return max(skipped, 1 + best(index + 1, used | pairs[index]))

    return best(0, set())


class EvaluatePairTest(unittest.TestCase):
    def test_two_short_loads_pair(self):
        outcome = evaluate_pair(make_trip("A1", 50), make_trip("B2", 50), NO_WINDOWS)
        self.assertIsInstance(outcome, Candidate)
        self.assertAlmostEqual(outcome.duty_hours, 8.0)
        self.assertAlmostEqual(outcome.drive_hours, 4.0)

    def test_combined_duty_over_fourteen_hours_is_rejected(self):
        # 8 h of duty each: legal apart, two hours over the limit together.
        outcome = evaluate_pair(
            make_trip("A1", 100, dwell=3.0), make_trip("B2", 100, dwell=3.0), NO_WINDOWS
        )
        self.assertNotIsInstance(outcome, Candidate)
        self.assertIn("duty", outcome.reason)

    def test_combined_drive_over_eleven_hours_is_rejected(self):
        # 6 h driving each, but only 30 minutes of dwell, so duty stays legal.
        config = PairingConfig(dc_zip=DC, max_duty_hours=24.0, windows=WindowPolicy(enforce=False))
        outcome = evaluate_pair(
            make_trip("A1", 150, dwell=0.5), make_trip("B2", 150, dwell=0.5), config
        )
        self.assertNotIsInstance(outcome, Candidate)
        self.assertIn("drive", outcome.reason)

    def test_equipment_is_only_checked_when_the_flag_is_on(self):
        loose = evaluate_pair(make_trip("A1", 50), make_trip("B2", 50, equipment="53RL"), NO_WINDOWS)
        self.assertIsInstance(loose, Candidate)

        strict = PairingConfig(dc_zip=DC, match_equipment=True, windows=WindowPolicy(enforce=False))
        outcome = evaluate_pair(make_trip("A1", 50), make_trip("B2", 50, equipment="53RL"), strict)
        self.assertNotIsInstance(outcome, Candidate)
        self.assertIn("equipment", outcome.reason)

    def test_a_morning_window_load_is_put_first(self):
        config = PairingConfig(dc_zip=DC, windows=WindowPolicy(earliest_start=4.0))
        early = make_trip("A1", 50, windows=(5.75, 7.0))
        late = make_trip("B2", 50, windows=(11.0, 20.0))
        outcome = evaluate_pair(early, late, config)
        self.assertIsInstance(outcome, Candidate)
        self.assertEqual(outcome.load_ids, ("A1", "B2"))

    def test_two_loads_that_both_need_the_first_turn_cannot_pair(self):
        config = PairingConfig(dc_zip=DC, windows=WindowPolicy(earliest_start=4.0))
        outcome = evaluate_pair(
            make_trip("A1", 50, windows=(5.25, 6.5)), make_trip("B2", 50, windows=(5.75, 6.75)), config
        )
        self.assertNotIsInstance(outcome, Candidate)


class ResequenceTest(unittest.TestCase):
    """The sheet's stop order is a suggestion; a delivery time is not."""

    def setUp(self):
        # Two stops an hour out in opposite directions. Sheet order sends the
        # driver to the late one first, so the early one is missed -- which is
        # the shape of a real "cannot reach X by HH:MM" rejection.
        self.late = Stop(order="1", store="Late", zip="90001", window_open=6.0, window_close=14.0)
        self.early = Stop(order="2", store="Early", zip="90002", window_open=6.0, window_close=8.0)
        self.miles = {
            ("01020", "90001"): 50.0, ("90001", "01020"): 50.0,
            ("01020", "90002"): 50.0, ("90002", "01020"): 50.0,
            ("90001", "90002"): 150.0, ("90002", "90001"): 150.0,
        }
        self.config = PairingConfig(dc_zip=DC)

    def miles_for(self, a, b):
        return self.miles[(a, b)], "estimated"

    def load(self, *stops):
        return Load(load_id="L1", carrier_id="PTAG", equipment="53LG", stops=tuple(stops))

    def test_sheet_order_that_misses_a_delivery_time_is_reordered(self):
        trips = build_trips(
            [self.load(self.late, self.early)], self.miles_for, lambda _z: 1.0, self.config
        )
        self.assertTrue(trips[0].resequenced)
        self.assertEqual([s.store for s in trips[0].load.stops], ["Early", "Late"])
        self.assertTrue(plan(trips, self.config).assignments)

    def test_a_workable_sheet_order_is_left_alone(self):
        trips = build_trips(
            [self.load(self.early, self.late)], self.miles_for, lambda _z: 1.0, self.config
        )
        self.assertFalse(trips[0].resequenced)
        self.assertEqual([s.store for s in trips[0].load.stops], ["Early", "Late"])

    def test_keeping_the_sheet_order_lets_the_load_fail(self):
        config = PairingConfig(dc_zip=DC, resequence=False)
        trips = build_trips([self.load(self.late, self.early)], self.miles_for, lambda _z: 1.0, config)
        self.assertFalse(trips[0].resequenced)
        result = plan(trips, config)
        self.assertEqual([r.load_ids for r in result.unschedulable], [("L1",)])
        self.assertIn("cannot reach Early", result.unschedulable[0].reason)

    def test_the_plan_names_what_it_reordered(self):
        trips = build_trips(
            [self.load(self.late, self.early)], self.miles_for, lambda _z: 1.0, self.config
        )
        result = plan(trips, self.config)
        self.assertEqual([t.load.load_id for t in result.resequenced], ["L1"])

    def test_no_order_works_means_the_load_still_fails(self):
        impossible = Stop(order="2", store="Impossible", zip="90002",
                          window_open=0.0, window_close=0.5)
        trips = build_trips(
            [self.load(self.late, impossible)], self.miles_for, lambda _z: 1.0, self.config
        )
        result = plan(trips, self.config)
        self.assertTrue(result.unschedulable)

    def test_stops_are_ranked_by_when_they_are_due(self):
        self.assertEqual(
            [s.store for s in delivery_order([self.late, self.early])], ["Early", "Late"]
        )
        undated = Stop(order="3", store="Anytime", zip="90003")
        self.assertEqual(
            [s.store for s in delivery_order([undated, self.late, self.early])],
            ["Early", "Late", "Anytime"],
        )


class PlanTest(unittest.TestCase):
    def test_every_load_lands_on_exactly_one_driver(self):
        trips = [make_trip(f"L{i}", 40 + 10 * i) for i in range(6)]
        result = plan(trips, NO_WINDOWS)
        assigned = [load_id for a in result.assignments for load_id in a.load_ids]
        self.assertEqual(sorted(assigned), sorted(t.load.load_id for t in trips))
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_pairing_halves_the_driver_count_when_everything_fits(self):
        trips = [make_trip(f"L{i}", 50) for i in range(6)]
        result = plan(trips, NO_WINDOWS)
        self.assertEqual(result.drivers, 3)
        self.assertEqual(len(result.pairs), 3)
        self.assertEqual(len(result.solos), 0)

    def test_a_load_too_long_for_one_shift_runs_with_a_layover(self):
        # 500 round-trip miles is 10 h driving, plus 4 h on the dock and 1 h
        # loading: legal to drive, more than one shift holds.
        trips = [make_trip("L0", 50), make_trip("L1", 250, dwell=4.0)]
        result = plan(trips, NO_WINDOWS)

        self.assertEqual(result.unschedulable, ())
        self.assertEqual([a.load_ids for a in result.layovers], [("L1",)])
        layover = result.layovers[0]
        self.assertEqual(layover.shifts, 2)
        self.assertAlmostEqual(layover.rest_hours, 10.0)
        self.assertAlmostEqual(layover.duty_hours, 15.0)          # rest is not duty
        self.assertAlmostEqual(layover.finish_hour - layover.start_hour, 25.0)
        self.assertEqual(result.load_count, 2)
        self.assertEqual(sorted(a.load_ids for a in result.assignments), [("L0",), ("L1",)])

    def test_a_load_over_the_drive_limit_also_runs_with_a_layover(self):
        # 12 h of driving cannot be done in one shift, but it can be done.
        trips = [make_trip("L1", 300, dwell=0.5)]
        result = plan(trips, NO_WINDOWS)
        self.assertEqual(result.unschedulable, ())
        self.assertEqual(result.drivers, 1)
        self.assertTrue(result.assignments[0].is_layover)
        self.assertGreaterEqual(result.assignments[0].shifts, 2)

    def test_a_layover_load_is_never_paired(self):
        trips = [make_trip("L0", 25), make_trip("L1", 300, dwell=0.5)]
        result = plan(trips, NO_WINDOWS)
        self.assertEqual(result.pairs, ())
        self.assertEqual(result.drivers, 2)

    def test_an_odd_load_out_runs_solo(self):
        trips = [make_trip("L0", 50), make_trip("L1", 50), make_trip("L2", 50)]
        result = plan(trips, NO_WINDOWS)
        self.assertEqual(result.drivers, 2)
        self.assertEqual(len(result.pairs), 1)
        self.assertEqual(len(result.solos), 1)

    def test_enforcing_windows_costs_pairs_and_that_is_expected(self):
        config = PairingConfig(dc_zip=DC, windows=WindowPolicy(earliest_start=4.0))
        # Three loads open early enough that each has to be the first turn out;
        # only one of them can take the fourth load as a second turn.
        trips = [
            make_trip("L0", 50, windows=(5.25, 6.5)),
            make_trip("L1", 50, windows=(5.75, 6.75)),
            make_trip("L2", 50, windows=(6.0, 7.0)),
            make_trip("L3", 50, windows=(11.0, 20.0)),
        ]
        ignored = plan(trips, PairingConfig(dc_zip=DC, windows=WindowPolicy(enforce=False)))
        enforced = plan(trips, config)
        self.assertEqual(ignored.drivers, 2)
        self.assertEqual(enforced.drivers, 3)
        self.assertLess(len(enforced.candidates), len(ignored.candidates))

    def test_the_plan_says_when_its_mileage_is_only_estimated(self):
        result = plan([make_trip("L0", 50)], NO_WINDOWS)
        self.assertTrue(result.estimated_mileage)

    def test_the_plan_pairs_as_many_loads_as_the_constraints_allow(self):
        # Long loads only fit alongside short ones, so the choice of partner
        # for each long load decides whether anything is left over.
        trips = [
            make_trip("L0", 275, dwell=0.5),    # 11 h drive... too long to pair
            make_trip("L1", 150, dwell=0.5),    # 6 h drive, 7.5 h duty
            make_trip("L2", 150, dwell=0.5),
            make_trip("L3", 25, dwell=0.5),     # 1 h drive, 2.5 h duty
            make_trip("L4", 25, dwell=0.5),
        ]
        config = PairingConfig(dc_zip=DC, windows=WindowPolicy(enforce=False))
        result = plan(trips, config)
        self.assertEqual(len(result.pairs), _max_pairs(result))
        assigned = [load_id for a in result.assignments for load_id in a.load_ids]
        self.assertEqual(len(assigned), len(set(assigned)))


if __name__ == "__main__":
    unittest.main()
