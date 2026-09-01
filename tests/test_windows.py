import unittest

from loadpairing.costing import cost_trip
from loadpairing.models import Load, Stop
from loadpairing.windows import END_OF_DAY, WindowPolicy, anchored_start, flatten, schedule

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

    def test_a_delivery_time_the_driver_cannot_make_is_infeasible(self):
        policy = WindowPolicy(earliest_start=9.0)
        result = schedule([trip([stop(open_at=5.0, close_at=9.5)])], policy)
        self.assertFalse(result.feasible)
        self.assertIn("cannot reach", result.reason)

    def test_the_reason_names_the_deadline_and_the_earliest_arrival(self):
        policy = WindowPolicy(earliest_start=9.0)
        result = schedule([trip([stop(open_at=5.0, close_at=9.5)])], policy)
        self.assertIn("by 09:30", result.reason)        # the delivery time
        self.assertIn("09:00", result.reason)           # when the driver could leave
        self.assertIn("11:00", result.reason)           # when they would get there

    def test_the_start_is_anchored_on_the_first_stop_opening(self):
        # 1 h loading plus a 1 h run out, so a 06:00 opening means a 04:00 start.
        one = trip([stop(open_at=6.0, close_at=10.0)])
        tasks, _tail = flatten([one], WindowPolicy())
        self.assertAlmostEqual(anchored_start(tasks), 4.0)

        result = schedule([one], WindowPolicy())
        self.assertAlmostEqual(result.start_hour, 4.0)
        self.assertAlmostEqual(result.stops[0].arrive, 6.0)

    def test_a_later_stop_may_still_have_to_wait(self):
        # Anchoring on the first stop is the earliest the trip can progress;
        # a second stop that opens much later is waited on all the same.
        early = stop(open_at=6.0, close_at=7.0, store="Early")
        late = Stop(order="2", store="Late", zip="01013", window_open=14.0, window_close=20.0)
        one = trip([early, late])
        result = schedule([one], WindowPolicy())
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.start_hour, 4.0)
        self.assertAlmostEqual(result.stops[0].arrive, 6.0)     # exactly as it opens
        self.assertAlmostEqual(result.stops[1].arrive, 8.0)
        self.assertAlmostEqual(result.stops[1].wait, 6.0)       # held until 14:00
        self.assertAlmostEqual(result.duty_hours, one.duty_hours + 6.0)

    def test_a_start_the_operation_forbids_falls_back(self):
        # A 05:00 opening would need a 03:00 start; dispatch opens at 04:00,
        # so the day starts then and the stop is reached after it opens.
        one = trip([stop(open_at=5.0, close_at=12.0)])
        result = schedule([one], WindowPolicy(earliest_start=4.0))
        self.assertTrue(result.feasible)
        self.assertGreaterEqual(result.start_hour, 4.0)
        self.assertGreaterEqual(result.stops[0].arrive, 5.0)
        self.assertAlmostEqual(result.stops[0].wait, 0.0)

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


class DeliveryTimeTest(unittest.TestCase):
    """Window Close is the delivery time, and 00:00 means the end of the day."""

    def test_a_midnight_close_is_due_by_2359_that_night(self):
        policy = WindowPolicy()
        self.assertEqual(policy.window_for(stop(open_at=0.0, close_at=0.0)), (0.0, END_OF_DAY))
        self.assertEqual(policy.window_for(stop(open_at=6.0, close_at=0.0)), (6.0, END_OF_DAY))

    def test_a_load_due_by_end_of_day_can_run_any_time(self):
        result = schedule([trip([stop(open_at=0.0, close_at=0.0)])], WindowPolicy(earliest_start=6.0))
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.start_hour, 6.0)

    def test_end_of_day_is_still_a_deadline(self):
        # Dispatched at 22:00 the driver cannot be there before midnight.
        result = schedule([trip([stop(open_at=0.0, close_at=0.0)])], WindowPolicy(earliest_start=22.5))
        self.assertFalse(result.feasible)

    def test_a_real_close_is_the_hour_the_load_is_due(self):
        result = schedule([trip([stop(open_at=6.0, close_at=10.0)])], WindowPolicy())
        self.assertTrue(result.feasible)
        self.assertLessEqual(result.stops[0].arrive, 10.0)

    def test_an_ordinary_window_running_past_midnight_still_wraps(self):
        self.assertEqual(WindowPolicy().window_for(stop(open_at=22.0, close_at=2.0)), (22.0, 26.0))


class PairScheduleTest(unittest.TestCase):
    def test_the_second_turn_is_held_at_the_dc_not_at_the_receiver(self):
        # Turn one finishes early; rather than park at the customer's door the
        # driver waits at the DC and arrives as the second stop opens.
        first = trip([stop(store="A", open_at=6.0, close_at=8.0)], load_id="A")
        second = trip([stop(store="B", zip_code="01013", open_at=14.0, close_at=18.0)], load_id="B")
        result = schedule([first, second], WindowPolicy())
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.stops[0].arrive, 6.0)
        self.assertAlmostEqual(result.stops[1].arrive, 14.0)
        self.assertAlmostEqual(result.stops[1].wait, 0.0)
        self.assertAlmostEqual(result.finish_hour, 16.0)

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
