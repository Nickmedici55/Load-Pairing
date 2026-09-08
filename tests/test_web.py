import base64
import io
import os
import re
import tempfile
import unittest
from wsgiref.util import setup_testing_defaults

from loadpairing import web
from loadpairing.formdata import FormError, parse_form
from tests.fixtures import dispatch_sheet, hours, write_workbook

BOUNDARY = "----loadpairingtest"

CENTROIDS = """zip,lat,lon
01020,42.1487,-72.6079
01040,42.2043,-72.6162
01013,42.1626,-72.6076
01085,42.1362,-72.7573
12901,44.6995,-73.4529
12946,44.2795,-73.9860
"""

LOADS = [
    {"load_id": "10375774", "equipment": "53LG", "stops": [
        {"store": "Holyoke", "zip": 1040, "city": "Holyoke", "state": "MA",
         "open": hours(11.0), "close": hours(20.0), "pallets": 21}]},
    {"load_id": "10375775", "equipment": "53RL", "stops": [
        {"store": "Chicopee St", "zip": 1013, "city": "Chicopee", "state": "MA",
         "open": hours(11.0), "close": hours(20.0), "pallets": 24}]},
    {"load_id": "10375790", "equipment": "53PLG", "stops": [
        {"store": "Westfield DC", "zip": 1085, "city": "Westfield", "state": "MA",
         "open": hours(0.0), "close": hours(0.0), "pallets": 28}]},
    {"load_id": "10375781", "equipment": "53RL", "stops": [
        {"store": "Plattsburgh", "zip": 12901, "city": "Plattsburgh", "state": "NY",
         "open": hours(5.25), "close": hours(9.0), "pallets": 14},
        {"store": "Lake Placid", "zip": 12946, "city": "Lake Placid", "state": "NY", "pallets": 12}]},
]


def multipart(fields: dict, files: dict = ()) -> tuple[bytes, str]:
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, (filename, payload) in dict(files).items():
        parts.append(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + payload
            + b"\r\n"
        )
    parts.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={BOUNDARY}"


class FormDataTest(unittest.TestCase):
    def test_reads_text_fields_and_files(self):
        body, content_type = multipart({"carrier": "PTAG"}, {"sheet": ("a.xlsx", b"PK\x03\x04data")})
        form = parse_form(body, content_type)
        self.assertEqual(form.get("carrier"), "PTAG")
        self.assertEqual(form.file("sheet").filename, "a.xlsx")
        self.assertEqual(form.file("sheet").value, b"PK\x03\x04data")

    def test_an_empty_file_input_is_not_a_file(self):
        body, content_type = multipart({}, {"sheet": ("", b"")})
        self.assertIsNone(parse_form(body, content_type).file("sheet"))

    def test_unchecked_boxes_are_simply_absent(self):
        form = parse_form(b"enforce_windows=on", "application/x-www-form-urlencoded")
        self.assertTrue(form.checked("enforce_windows"))
        self.assertFalse(form.checked("match_equipment"))

    def test_an_unknown_content_type_is_refused(self):
        with self.assertRaises(FormError):
            parse_form(b"{}", "application/json")


class WebAppTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sheet = os.path.join(self.dir, "NESC.xlsx")
        write_workbook(
            self.sheet,
            {"Summary_1": [["t"]], "Reverse Order_2": [["r"]], "Dispatch Order_3": dispatch_sheet(LOADS)},
        )
        with open(self.sheet, "rb") as handle:
            self.workbook = handle.read()

        self._env = dict(os.environ)
        os.environ["LOAD_PAIRING_DB"] = os.path.join(self.dir, "web.sqlite3")
        os.environ["LOAD_PAIRING_ROUTER"] = "estimated"
        for key in ("PCMILER_API_KEY", "GOOGLE_MAPS_API_KEY", "HERE_API_KEY"):
            os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def request(self, path, method="GET", body=b"", content_type="", auth=None):
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": method,
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
            }
        )
        if auth is not None:
            environ["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(auth.encode()).decode()
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        chunks = web.application(environ, start_response)
        return captured["status"], b"".join(chunks).decode("utf-8")

    def prime_coordinates(self):
        """The real sequence: a sheet records its ZIPs, then coordinates fill them in."""
        self.post_plan()
        body, content_type = multipart({}, {"centroids": ("c.csv", CENTROIDS.encode())})
        return self.request("/coordinates", "POST", body, content_type)

    def post_plan(self, **overrides):
        fields = {"tab": "3", "carrier": "PTAG", "dc_zip": "01020", "earliest_start": "4",
                  "max_duty": "14", "max_drive": "11",
                  "objective": "duty", "enforce_windows": "on"}
        fields.update(overrides)
        # An unticked checkbox is absent from a real submission, not empty.
        fields = {name: value for name, value in fields.items() if value is not None}
        body, content_type = multipart(fields, {"sheet": ("NESC.xlsx", self.workbook)})
        return self.request("/plan", "POST", body, content_type)

    def test_the_upload_form_is_the_front_page(self):
        status, html = self.request("/")
        self.assertEqual(status, "200 OK")
        self.assertIn('action="/plan"', html)
        self.assertIn("Dispatch workbook", html)

    def test_the_carrier_field_starts_on_ptag(self):
        _status, html = self.request("/")
        self.assertIn('name="carrier" value="PTAG"', html)

    def test_a_load_over_one_shift_is_shown_as_a_layover_not_a_failure(self):
        self.prime_coordinates()
        status, html = self.post_plan()
        self.assertEqual(status, "200 OK")
        self.assertIn("10375781", html)          # the Adirondack run
        self.assertIn("layover", html)
        self.assertNotIn("exceeds the 11 h limit", html)

    def test_a_midnight_delivery_time_shows_as_a_drop_and_hook(self):
        self.prime_coordinates()
        status, html = self.post_plan()
        self.assertEqual(status, "200 OK")
        self.assertIn("10375790", html)
        self.assertIn("D&amp;H", html)          # escaped in the window column

    def test_health_check(self):
        status, body = self.request("/healthz")
        self.assertEqual(status, "200 OK")
        self.assertEqual(body, "ok")

    def test_an_unknown_path_is_a_404(self):
        status, _ = self.request("/nope")
        self.assertEqual(status, "404 Not Found")

    def test_posting_without_a_sheet_asks_for_one(self):
        body, content_type = multipart({"tab": "3"})
        status, html = self.request("/plan", "POST", body, content_type)
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Choose a dispatch workbook", html)

    def test_a_sheet_with_no_matching_carrier_names_the_tabs(self):
        status, html = self.post_plan(carrier="NOPE")
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("No loads matched", html)
        self.assertIn("Dispatch Order_3", html)

    def test_without_coordinates_the_estimate_explains_itself(self):
        status, html = self.post_plan()
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("PCMILER_API_KEY", html)

    def test_a_plan_renders_drivers_and_their_stops(self):
        self.prime_coordinates()
        status, html = self.post_plan()
        self.assertEqual(status, "200 OK")
        self.assertIn("Driver 1", html)
        self.assertIn("10375774", html)
        self.assertIn("Holyoke", html)
        self.assertIn("estimated mileage", html)
        # The Adirondack run cannot make its 05:15-09:00 window on these miles.
        self.assertIn("Unschedulable", html)
        self.assertIn("10375781", html)

    def test_uploading_a_sheet_records_its_locations(self):
        self.post_plan()
        status, html = self.request("/locations")
        self.assertEqual(status, "200 OK")
        self.assertIn("12946", html)
        self.assertIn('name="dwell:01040"', html)
        self.assertIn("<th>Store</th>", html)
        # The store delivered at that ZIP, which the city column does not give.
        self.assertIn("Westfield DC", html)

    def test_a_saved_dwell_holds_and_changes_the_next_plan(self):
        self.prime_coordinates()
        status, html = self.request(
            "/locations", "POST", b"dwell:01040=3.5", "application/x-www-form-urlencoded"
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("Saved 1 dwell change", html)
        self.assertIn('value="3.50"', html)

        _status, planned = self.post_plan()
        hours_shown = [float(h) for h in re.findall(r"([\d.]+) h duty", planned)]
        self.assertTrue(any(h >= 6.0 for h in hours_shown), planned)

    def test_lanes_are_listed_and_flagged_as_estimated(self):
        self.prime_coordinates()
        self.post_plan()
        status, html = self.request("/lanes")
        self.assertEqual(status, "200 OK")
        self.assertIn("01020", html)
        self.assertIn("still estimated", html)

    def test_ignoring_windows_is_reported_on_the_plan(self):
        self.prime_coordinates()
        status, html = self.post_plan(enforce_windows=None)
        self.assertEqual(status, "200 OK")
        self.assertIn("windows ignored", html)

    def test_a_get_on_a_post_only_route_is_refused(self):
        status, _ = self.request("/plan")
        self.assertEqual(status, "405 Method Not Allowed")


class BasicAuthTest(unittest.TestCase):
    """With a password set every page but the health check needs it."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["LOAD_PAIRING_DB"] = os.path.join(tempfile.mkdtemp(), "auth.sqlite3")
        os.environ["LOAD_PAIRING_PASSWORD"] = "s3cret"
        os.environ.pop("LOAD_PAIRING_USER", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def get(self, path="/", header=None):
        environ = {}
        setup_testing_defaults(environ)
        environ.update({"PATH_INFO": path, "REQUEST_METHOD": "GET",
                        "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")})
        if header is not None:
            environ["HTTP_AUTHORIZATION"] = header
        captured = {}
        chunks = web.application(environ, lambda status, headers: captured.update(
            status=status, headers=dict(headers)))
        return captured["status"], captured["headers"], b"".join(chunks).decode("utf-8")

    def credentials(self, pair):
        return "Basic " + base64.b64encode(pair.encode()).decode()

    def test_the_app_is_open_when_no_password_is_set(self):
        os.environ.pop("LOAD_PAIRING_PASSWORD")
        self.assertEqual(self.get()[0], "200 OK")

    def test_a_request_without_credentials_is_challenged(self):
        status, headers, _ = self.get()
        self.assertEqual(status, "401 Unauthorized")
        self.assertIn("Basic", headers["WWW-Authenticate"])

    def test_the_health_check_stays_open_for_the_platform(self):
        self.assertEqual(self.get("/healthz")[0], "200 OK")

    def test_the_right_password_gets_in(self):
        self.assertEqual(self.get("/", self.credentials("dispatch:s3cret"))[0], "200 OK")

    def test_a_wrong_password_or_user_does_not(self):
        self.assertEqual(self.get("/", self.credentials("dispatch:nope"))[0], "401 Unauthorized")
        self.assertEqual(self.get("/", self.credentials("someone:s3cret"))[0], "401 Unauthorized")

    def test_the_user_name_is_configurable(self):
        os.environ["LOAD_PAIRING_USER"] = "nick"
        self.assertEqual(self.get("/", self.credentials("nick:s3cret"))[0], "200 OK")
        self.assertEqual(self.get("/", self.credentials("dispatch:s3cret"))[0], "401 Unauthorized")

    def test_a_malformed_header_is_rejected_not_crashed(self):
        for header in ("Basic !!!notbase64", "Bearer token", "", "Basic", "Basic " + base64.b64encode(b"\xff\xfe").decode()):
            self.assertEqual(self.get("/", header)[0], "401 Unauthorized", header)


if __name__ == "__main__":
    unittest.main()
