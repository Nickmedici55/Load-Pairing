import unittest

from loadpairing.costing import DROP_AND_HOOK_HOURS, cost_trip, round_trip_zips, stop_dwell
from loadpairing.models import Load, Stop

DC = "01020"


def load(*zips, equipment="53LG", load_id="L1"):
    stops = tuple(Stop(order=str(i + 1), store=f"S{i + 1}", zip=z) for i, z in enumerate(zips))
    return Load(load_id=load_id, carrier_id="PTAG", equipment=equipment, stops=stops)


class RoundTripTest(unittest.TestCase):
    def test_a_load_leaves_the_dc_and_comes_back_to_it(self):
        self.assertEqual(
            round_trip_zips(DC, load("01040", "01013")),
            (("01020", "01040"), ("01040", "01013"), ("01013", "01020")),
        )


class CostTripTest(unittest.TestCase):
    def miles(self, from_zip, to_zip):
        return 25.0, "estimated"

    def test_duration_is_load_plus_dwell_plus_miles_over_fifty(self):
        trip = cost_trip(load("01040", "01013"), DC, self.miles, lambda _z: 1.0)
        self.assertEqual(trip.miles, 75.0)
        self.assertAlmostEqual(trip.drive_hours, 1.5)
        self.assertAlmostEqual(trip.stop_hours, 2.0)
        self.assertAlmostEqual(trip.duty_hours, 1.0 + 2.0 + 1.5)

    def test_dwell_overrides_apply_per_location(self):
        dwell = {"01040": 2.5, "01013": 0.5}
        trip = cost_trip(load("01040", "01013"), DC, self.miles, lambda z: dwell[z])
        self.assertEqual(trip.dwell_hours, (2.5, 0.5))
        self.assertAlmostEqual(trip.duty_hours, 1.0 + 3.0 + 1.5)

    def test_a_drop_and_hook_is_half_an_hour(self):
        # A 00:00 delivery time marks the stop as a trailer swap.
        swap = Stop(order="1", store="S1", zip="01040", window_open=0.0, window_close=0.0)
        live = Stop(order="2", store="S2", zip="01013", window_open=11.0, window_close=20.0)
        load = Load(load_id="L1", carrier_id="PTAG", equipment="53LG", stops=(swap, live))
        trip = cost_trip(load, DC, self.miles, lambda _z: 1.0)
        self.assertEqual(trip.dwell_hours, (DROP_AND_HOOK_HOURS, 1.0))

    def test_a_drop_and_hook_beats_the_location_override(self):
        # The dispatcher's 3 h describes a live unload there, not a swap.
        swap = Stop(order="1", store="S1", zip="01040", window_open=0.0, window_close=0.0)
        self.assertEqual(stop_dwell(swap, lambda _z: 3.0), DROP_AND_HOOK_HOURS)

        live = Stop(order="1", store="S1", zip="01040", window_open=11.0, window_close=20.0)
        self.assertEqual(stop_dwell(live, lambda _z: 3.0), 3.0)

    def test_a_stop_with_no_window_at_all_is_not_a_drop_and_hook(self):
        blank = Stop(order="1", store="S1", zip="01040")
        self.assertFalse(blank.is_drop_and_hook)
        self.assertEqual(stop_dwell(blank, lambda _z: 1.25), 1.25)

    def test_a_trip_knows_when_its_mileage_is_only_an_estimate(self):
        trip = cost_trip(load("01040"), DC, self.miles, lambda _z: 1.0)
        self.assertTrue(trip.estimated)
        real = cost_trip(load("01040"), DC, lambda a, b: (25.0, "pcmiler"), lambda _z: 1.0)
        self.assertFalse(real.estimated)


if __name__ == "__main__":
    unittest.main()
