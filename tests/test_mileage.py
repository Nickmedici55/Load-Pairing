import unittest

from loadpairing.db import Lane, Location, connect
from loadpairing.geocode import estimated_miles, haversine_miles, read_centroids
from loadpairing.mileage import EstimatedRouter, MileageService, RoutingError


class FlakyRouter:
    name = "pcmiler"

    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.calls = 0

    def miles(self, origin, destination):
        self.calls += 1
        if (origin.zip, destination.zip) in self.fail_for:
            raise RoutingError("service unavailable")
        return 42.0


class MileageServiceTest(unittest.TestCase):
    def setUp(self):
        self.store = connect(":memory:")
        self.store.ensure_locations(
            [
                Location("01020", "Chicopee", "MA", lat=42.1487, lon=-72.6079),
                Location("01040", "Holyoke", "MA", lat=42.2043, lon=-72.6162),
            ]
        )

    def tearDown(self):
        self.store.close()

    def test_a_lane_is_fetched_once_and_cached_for_good(self):
        router = FlakyRouter()
        service = MileageService(self.store, router)
        self.assertEqual(service.miles("01020", "01040"), (42.0, "pcmiler"))
        self.assertEqual(service.miles("01020", "01040"), (42.0, "pcmiler"))
        self.assertEqual(router.calls, 1)
        self.assertEqual(service.fetched, 1)

        # A fresh service reads the cache rather than the router.
        again = MileageService(self.store, FlakyRouter())
        self.assertEqual(again.miles("01020", "01040"), (42.0, "pcmiler"))
        self.assertEqual(again.fetched, 0)

    def test_a_routing_failure_falls_back_to_the_offline_estimate(self):
        service = MileageService(self.store, FlakyRouter(fail_for={("01020", "01040")}))
        miles, source = service.miles("01020", "01040")
        self.assertEqual(source, "estimated")
        self.assertAlmostEqual(miles, estimated_miles(42.1487, -72.6079, 42.2043, -72.6162), places=1)
        self.assertIn(("01020", "01040"), service.failures)
        self.assertEqual(len(self.store.estimated_lanes()), 1)

    def test_a_stop_in_the_same_zip_as_the_dc_is_free(self):
        service = MileageService(self.store, FlakyRouter())
        self.assertEqual(service.miles("01020", "01020"), (0.0, "same-zip"))

    def test_the_offline_estimate_needs_coordinates(self):
        self.store.ensure_locations([Location("12946")])
        service = MileageService(self.store, EstimatedRouter())
        with self.assertRaises(RoutingError):
            service.miles("01020", "12946")


class GeocodeTest(unittest.TestCase):
    def test_great_circle_distance(self):
        self.assertAlmostEqual(haversine_miles(42.1487, -72.6079, 42.2043, -72.6162), 3.87, places=1)

    def test_the_estimate_adds_the_circuity_factor(self):
        straight = haversine_miles(42.1487, -72.6079, 44.2795, -73.9860)
        self.assertAlmostEqual(estimated_miles(42.1487, -72.6079, 44.2795, -73.9860), round(straight * 1.20, 1))

    def test_centroid_csv_tolerates_a_header_and_short_zips(self):
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "z.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("zip,lat,lon\n1040,42.2043,-72.6162\n12946,44.2795,-73.9860\nbad,row\n")
        centroids = read_centroids(path)
        self.assertEqual(sorted(centroids), ["01040", "12946"])
        self.assertAlmostEqual(centroids["01040"][0], 42.2043)


if __name__ == "__main__":
    unittest.main()
