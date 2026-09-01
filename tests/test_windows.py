import unittest

from loadpairing.costing import cost_trip
from loadpairing.models import Load, Stop
from loadpairing.windows import MIDNIGHT_STRICT, WindowPolicy, schedule

DC = "01020"


def trip(stops, miles=50.0, load_id="L1", dwell=1.0):
    stops = tuple(stops)
    load = Load(load_id=load_id, carrier_id="PTAG", equipment="53LG", stops=stops)
    return cost_trip(load, DC, lambda a, b: (miles, "estimated"), lambda _z: dwell)


def stop(zip_code="01040", open_at=None, close_at=None, store="Store"):
    return Stop(order="1", store=store, zip=zip_code, window_open=open_at, window_close=close_at)


class SoloScheduleTest(unittest.TestCase):
    def test_a_load_with_no_windows_starts_at_the_earliest_hour_allowed(self):
        result = schedule([trip([stop()])], WindowPolicy(earliest_start=4.0))
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.start_hour, 4.0)
        self.assertAlmostEqual(result.duty_hours, 4.0)   # 1 load + 1 dwell + 2 drive

    def test_the_day_starts_late_enough_to_avoid_waiting_on_a_window(self):
        result = schedule([trip([stop(open_at=6.0, close_at=10.0)])], WindowPolicy())
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.start_hour, 4.0)   # 1 h loading + 1 h out lands on 06:00
        self.assertAlmostEqual(result.stops[0].arrive, 6.0)
        self.assertAlmostEqual(result.stops[0].wait, 0.0)

    def test_a_window_that_closes_before_the_driver_can_get_there_is_infeasible(self):
        policy = WindowPolicy(earliest_start=9.0)
        result = schedule([trip([stop(open_at=5.0, close_at=9.5)])], policy)
        self.assertFalse(result.feasible)
        self.assertIn("window closes", result.reason)

    def test_waiting_is_only_taken_when_two_windows_leave_no_choice(self):
        early = stop(open_at=6.0, close_at=7.0, store="Early")
        late = Stop(order="2", store="Late", zip="01013", window_open=14.0, window_close=20.0)
        one = trip([early, late])
        result = schedule([one], WindowPolicy())
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.stops[0].arrive, 7.0)     # as late as the first window allows
        self.assertAlmostEqual(result.stops[1].arrive, 9.0)
        self.assertAlmostEqual(result.stops[1].wait, 5.0)       # held until 14:00
        self.assertAlmostEqual(result.duty_hours, one.duty_hours + 5.0)

    def test_the_driver_is_not_held_at_the_dc_when_waiting_can_be_avoided(self):
        first = stop(open_at=0.0, close_at=24.0, store="Anytime")
        second = Stop(order="2", store="Late", zip="01013", window_open=14.0, window_close=16.0)
        result = schedule([trip([first, second])], WindowPolicy(earliest_start=8.0))
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.stops[1].arrive, 14.0)
        self.assertAlmostEqual(result.stops[1].wait, 0.0)

    def test_a_window_running_past_midnight_is_read_as_overnight(self):
        result = schedule([trip([stop(open_at=22.0, close_at=2.0)])], WindowPolicy())
        self.assertTrue(result.feasible)
        self.assertGreaterEqual(result.stops[0].arrive, 22.0)


class MidnightPolicyTest(unittest.TestCase):
    def test_zero_to_zero_is_read_as_no_window_by_default(self):
        result = schedule([trip([stop(open_at=0.0, close_at=0.0)])], WindowPolicy(earliest_start=6.0))
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.start_hour, 6.0)

    def test_zero_to_zero_can_be_enforced_literally(self):
        policy = WindowPolicy(midnight=MIDNIGHT_STRICT, earliest_start=6.0)
        result = schedule([trip([stop(open_at=0.0, close_at=0.0)])], policy)
        self.assertFalse(result.feasible)


class PairScheduleTest(unittest.TestCase):
    def test_two_trips_run_back_to_back_through_the_dc(self):
        first = trip([stop(store="A")], load_id="A")
        second = trip([stop(store="B", zip_code="01013")], load_id="B")
        result = schedule([first, second], WindowPolicy())
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.duty_hours, 8.0)          # two 4 h trips, no waiting
        self.assertEqual([s.load_id for s in result.stops], ["A", "B"])
        self.assertAlmostEqual(result.stops[1].arrive - result.stops[0].depart, 3.0)

    def test_a_morning_window_load_has_to_be_the_first_turn_out(self):
        # A load whose first stop opens at 05:00 and closes at 06:30 can be run
        # solo, but not behind another turn: the driver cannot start early
        # enough to be back at the DC, reloaded and there in time.
        policy = WindowPolicy(earliest_start=4.0)
        morning = Stop(order="1", store="Morning", zip="01013", window_open=5.0, window_close=6.5)
        first = trip([stop(store="A")], load_id="A")
        second = trip([morning], load_id="B")
        self.assertTrue(schedule([second], policy).feasible)
        self.assertTrue(schedule([second, first], policy).feasible)
        self.assertFalse(schedule([first, second], policy).feasible)


if __name__ == "__main__":
    unittest.main()
