import base64
import io
import os
import re
import tempfile
import unittest
from html import unescape
from urllib.parse import urlencode
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
        path, _, query = path.partition("?")     # the server splits these, not the app
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "PATH_INFO": path,
                "QUERY_STRING": query,
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

    def form_fields(self, html):
        """Everything the plan page's form would submit, as a browser would."""
        return {
            name: unescape(value)
            for name, value in re.findall(
                r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html
            )
        }

    def post_arrange(self, fields, action="arrange", **moves):
        """Re-submit the plan page with some loads moved to other drivers."""
        fields = dict(fields, action=action)
        for load_id, driver in moves.items():
            fields[f"driver:{load_id}"] = str(driver)
        return self.request(
            "/arrange", "POST", urlencode(fields).encode(),
            "application/x-www-form-urlencoded",
        )

    def planned(self):
        """A plan page ready to rearrange, and its form fields."""
        self.prime_coordinates()
        _status, html = self.post_plan()
        return html, self.form_fields(html)

    def drivers_in(self, html):
        """Which loads sit on which driver number, as the page renders it."""
        grouped = {}
        for load_id, driver in re.findall(
            r'name="driver:([^"]+)" value="(\d+)"', html
        ):
            grouped.setdefault(driver, []).append(load_id)
        return {driver: sorted(loads) for driver, loads in grouped.items()}

    def driver_of(self, html, load_id):
        for driver, loads in self.drivers_in(html).items():
            if load_id in loads:
                return driver
        raise AssertionError(f"{load_id} is not on the page")

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

    def test_a_load_that_misses_its_window_is_shown_with_the_reason(self):
        self.prime_coordinates()
        status, html = self.post_plan()
        self.assertEqual(status, "200 OK")
        self.assertIn("10375781", html)          # the Adirondack run
        self.assertIn("cannot be scheduled", html)
        self.assertIn("cannot reach Plattsburgh", html)
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
        # It stays on the page as a driver of its own so it can be moved.
        self.assertIn("10375781", html)
        self.assertIn("cannot be scheduled", html)

    def test_the_plan_names_the_service_center_it_ran_out_of(self):
        self.prime_coordinates()
        _status, html = self.post_plan()
        self.assertIn("Out of New England SC", html)

    def test_the_plan_form_offers_the_service_centers_as_a_dropdown(self):
        _status, html = self.request("/")
        self.assertIn('<select id="dc_zip" name="dc_zip">', html)
        self.assertIn("New England SC", html)

    def test_a_service_center_can_be_added_and_is_then_offered_for_upload(self):
        body = b"zip=06103&name=Hartford+SC&city=Hartford&state=ct"
        status, html = self.request(
            "/service-centers", "POST", body, "application/x-www-form-urlencoded"
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("Hartford SC (06103) added", html)

        _status, form = self.request("/")
        self.assertIn('value="06103"', form)
        self.assertIn("Hartford SC", form)

    def test_a_service_center_needs_a_zip_and_a_name(self):
        status, html = self.request(
            "/service-centers", "POST", b"zip=&name=", "application/x-www-form-urlencoded"
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("needs a ZIP and a name", html)

    def test_a_sheet_cannot_be_uploaded_against_an_unknown_service_center(self):
        status, html = self.post_plan(dc_zip="99999")
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("is not a service center", html)

    def test_stores_stay_under_the_service_center_the_sheet_was_uploaded_for(self):
        self.request(
            "/service-centers", "POST", b"zip=06103&name=Hartford+SC",
            "application/x-www-form-urlencoded",
        )
        self.post_plan()                      # the sheet runs out of 01020

        _status, hartford = self.request("/locations?sc=06103")
        self.assertIn("No sheet has been uploaded for Hartford SC yet", hartford)
        self.assertNotIn("12946", hartford)

        _status, chicopee = self.request("/locations?sc=01020")
        self.assertIn("12946", chicopee)

    def test_a_dwell_is_saved_against_the_service_center_it_was_shown_for(self):
        self.request(
            "/service-centers", "POST", b"zip=06103&name=Hartford+SC",
            "application/x-www-form-urlencoded",
        )
        self.post_plan()
        self.post_plan(dc_zip="06103")        # the same stores, from Hartford

        status, html = self.request(
            "/locations", "POST", b"service_center=06103&dwell:01040=3.5",
            "application/x-www-form-urlencoded",
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("Saved 1 dwell change", html)

        _status, chicopee = self.request("/locations?sc=01020")
        self.assertNotIn('value="3.50"', chicopee)

    def test_a_plan_comes_back_ready_to_rearrange(self):
        html, fields = self.planned()
        self.assertIn('action="/arrange"', html)
        self.assertIn('name="driver:10375774"', html)
        # Everything needed to re-cost the day without the spreadsheet.
        self.assertIn("loads", fields)
        self.assertIn("settings", fields)
        self.assertIn("baseline", fields)
        self.assertIn("10375774", fields["loads"])

    def test_moving_two_loads_onto_one_driver_re_costs_the_day(self):
        html, fields = self.planned()
        before = self.drivers_in(html)
        self.assertNotEqual(before["1"], sorted(["10375790", "10375774", "10375775"]))

        # Put the Westfield drop and hook on the same driver as the Holyoke pair.
        status, moved = self.post_arrange(
            fields, **{"10375790": 2, "10375774": 2, "10375775": 2}
        )

        self.assertEqual(status, "200 OK")
        after = self.drivers_in(moved)
        together = [loads for loads in after.values() if len(loads) == 3]
        self.assertEqual(together, [sorted(["10375774", "10375775", "10375790"])])
        self.assertIn("vs the built plan", moved)      # the comparison is shown

    def test_a_rearranged_page_can_be_rearranged_again(self):
        _html, fields = self.planned()
        _status, once = self.post_arrange(fields, **{"10375774": 2, "10375775": 2})
        self.assertEqual(self.driver_of(once, "10375774"), self.driver_of(once, "10375775"))

        # The page that came back carries what the next change needs.
        status, twice = self.post_arrange(self.form_fields(once), **{"10375775": 9})

        self.assertEqual(status, "200 OK")
        self.assertNotEqual(self.driver_of(twice, "10375774"), self.driver_of(twice, "10375775"))

    def test_splitting_a_pair_costs_a_driver(self):
        html, fields = self.planned()
        paired = [loads for loads in self.drivers_in(html).values() if len(loads) == 2]
        self.assertTrue(paired, "the fixture should pair two loads")
        first, second = paired[0]

        _status, split = self.post_arrange(fields, **{first: 8, second: 9})

        self.assertIn("vs the built plan", split)
        self.assertTrue(all(len(loads) == 1 for loads in self.drivers_in(split).values()))

    def test_a_driver_can_be_added_and_filled_from_the_page(self):
        _html, fields = self.planned()

        _status, added = self.post_arrange(fields, action="add")
        self.assertIn("no loads yet", added)
        empty = re.search(r'name="spare" value="(\d+)"', added)
        self.assertIsNotNone(empty, "the empty driver should survive the round trip")

        # Putting that number beside a load moves the load onto the new driver.
        status, filled = self.post_arrange(
            self.form_fields(added), **{"10375774": int(empty.group(1))}
        )

        self.assertEqual(status, "200 OK")
        self.assertNotIn("no loads yet", filled)          # the slot was taken up
        self.assertEqual(
            [loads for loads in self.drivers_in(filled).values() if loads == ["10375774"]],
            [["10375774"]],
        )

    def test_an_empty_driver_waits_around_until_it_is_used_or_removed(self):
        _html, fields = self.planned()
        _status, added = self.post_arrange(fields, action="add")

        # Re-planning without filling it leaves it there to be filled later.
        _status, again = self.post_arrange(self.form_fields(added))
        self.assertIn("no loads yet", again)

        number = re.search(r'name="spare" value="(\d+)"', again).group(1)
        _status, removed = self.post_arrange(self.form_fields(again), action=f"drop:{number}")
        self.assertNotIn("no loads yet", removed)

    def test_splitting_a_driver_gives_every_load_its_own(self):
        html, fields = self.planned()
        paired = [
            number for number, loads in self.drivers_in(html).items() if len(loads) > 1
        ]
        self.assertTrue(paired, "the fixture should pair two loads")

        status, split = self.post_arrange(fields, action=f"split:{paired[0]}")

        self.assertEqual(status, "200 OK")
        self.assertTrue(
            all(len(loads) == 1 for loads in self.drivers_in(split).values()),
            self.drivers_in(split),
        )
        self.assertIn("vs the built plan", split)

    def test_every_load_can_be_put_on_one_driver(self):
        html, fields = self.planned()
        every = {load_id: 1 for loads in self.drivers_in(html).values() for load_id in loads}

        status, one = self.post_arrange(fields, **every)

        self.assertEqual(status, "200 OK")
        self.assertEqual(len(self.drivers_in(one)), 1)
        self.assertIn("layover", one)          # more work than one shift holds

    def test_the_page_names_a_free_driver_number(self):
        html, _fields = self.planned()
        self.assertRegex(html, r"<strong>a number nobody else has</strong>")
        self.assertRegex(html, r"\d+ is\s*\n?free")

    def test_a_submission_that_is_not_a_plan_is_refused_cleanly(self):
        _html, fields = self.planned()
        status, html = self.post_arrange(dict(fields, loads="not a plan"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Upload the sheet again", html)

    def test_saving_needs_a_name(self):
        _html, fields = self.planned()
        status, html = self.post_arrange(fields, action="save")
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Give the plan a name", html)

    def test_a_plan_is_saved_under_a_name_and_can_be_reopened(self):
        _html, fields = self.planned()
        fields["plan_name"] = "Tuesday 8/28"

        status, saved = self.post_arrange(fields, action="save", **{"10375774": 5})
        self.assertEqual(status, "200 OK")
        self.assertIn("Saved as", saved)

        _status, listed = self.request("/plans")
        self.assertIn("Tuesday 8/28", listed)

        link = re.search(r'/plans/open\?id=([0-9a-f]+)', listed)
        self.assertIsNotNone(listed)
        status, reopened = self.request(f"/plans/open?id={link.group(1)}")
        self.assertEqual(status, "200 OK")
        self.assertIn("Tuesday 8/28", reopened)
        self.assertIn("10375774", reopened)
        # The load moved before saving is still on a driver of its own.
        alone = [loads for loads in self.drivers_in(reopened).values() if loads == ["10375774"]]
        self.assertEqual(len(alone), 1)

    def test_saving_over_a_name_replaces_that_plan(self):
        _html, fields = self.planned()
        fields["plan_name"] = "Tuesday"
        self.post_arrange(fields, action="save")
        _status, again = self.post_arrange(fields, action="save", **{"10375774": 7})

        self.assertIn("replacing the plan of that name", again)
        _status, listed = self.request("/plans")
        self.assertEqual(listed.count("/plans/open?id="), 1)

    def test_a_saved_plan_can_be_deleted(self):
        _html, fields = self.planned()
        fields["plan_name"] = "Scrap this"
        self.post_arrange(fields, action="save")
        _status, listed = self.request("/plans")
        plan_id = re.search(r'/plans/open\?id=([0-9a-f]+)', listed).group(1)

        status, after = self.request(
            "/plans/delete", "POST", urlencode({"id": plan_id}).encode(),
            "application/x-www-form-urlencoded",
        )

        self.assertEqual(status, "200 OK")
        self.assertIn("Plan deleted", after)
        self.assertNotIn("Scrap this", after)

    def test_opening_a_plan_that_is_gone_says_so(self):
        status, html = self.request("/plans/open?id=deadbeef")
        self.assertEqual(status, "404 Not Found")
        self.assertIn("no longer saved", html)

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
