"""Tests for the local web server: the actions, and the HTTP safety checks."""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from drive_rescue import plan as plan_mod, server  # noqa: E402
from test_engine import Tweaked  # noqa: E402
from test_clone import Pair  # noqa: E402
from drive_rescue import clone as clone_mod  # noqa: E402


class AppCase(unittest.TestCase):
    def setUp(self):
        self._root = plan_mod.MOUNT_ROOT
        self.sys = Tweaked()
        plan_mod.MOUNT_ROOT = os.path.join(self.sys.root, "mnt")
        self.app = server.App(self.sys)
        self.src, self.dst = self.sys.source_file, "/dev/fake-sdb1"

    def tearDown(self):
        self.app.shutdown()
        plan_mod.MOUNT_ROOT = self._root
        self.sys.cleanup()

    def call(self, method, route, body=None, query=None):
        return self.app.dispatch(method, route, query or {}, body or {})

    def wait_done(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            s = self.call("GET", "status")
            if s["state"] in ("done", "error", "stopped"):
                return s
            time.sleep(0.1)
        self.fail("copy did not finish")


class ActionTests(AppCase):
    def test_clean_opts_clamps(self):
        o = server.clean_opts({"style": "evil", "retries": 999, "direction": "x"})
        self.assertEqual(o, {"style": "gentle", "retries": 10, "direction": "forward", "pause_after_quick": False})
        self.assertTrue(server.clean_opts({"pause_after_quick": True})["pause_after_quick"])
        self.assertFalse(server.clean_opts({"pause_after_quick": "yes"})["pause_after_quick"])
        self.assertEqual(server.clean_opts("junk")["retries"], 3)

    def test_drives_and_unknown_route(self):
        self.assertTrue(self.call("GET", "drives")["drives"])
        with self.assertRaises(server.ApiError) as cm:
            self.call("GET", "nope")
        self.assertEqual(cm.exception.status, 404)

    def test_health_only_for_listed_drives(self):
        with self.assertRaises(server.ApiError):
            self.call("GET", "health", query={"path": "/etc/passwd"})

    def test_plan_ok_and_refusals(self):
        p = self.call("POST", "plan", {"source": self.src, "dest": self.dst, "folder": "DR"})
        self.assertTrue(p["ok"], p)
        bad = self.call("POST", "plan", {"source": self.src, "dest": "/dev/fake-sdc1"})
        self.assertFalse(bad["ok"])

    def test_unmounted_partition_smaller_than_the_source_is_refused_early(self):
        def shrink(devs):
            part = devs[1]["children"][0]
            part["size"], part["mountpoints"] = 1024 * 1024, []
        self.sys.tweak = shrink
        p = self.call("POST", "plan", {"source": self.src, "dest": self.dst})
        self.assertFalse(p["ok"])
        self.assertIn("can't fit", " ".join(p["errors"]))

    def test_unmounted_partition_big_enough_still_plans(self):
        self.sys.tweak = lambda devs: devs[1]["children"][0].update(mountpoints=[])
        self.assertTrue(self.call("POST", "plan", {"source": self.src, "dest": self.dst})["ok"])

    def test_start_refused_gives_errors(self):
        with self.assertRaises(server.ApiError) as cm:
            self.call("POST", "start", {"source": self.src, "dest": "/dev/fake-sdc1"})
        self.assertEqual(cm.exception.status, 400)
        self.assertTrue(cm.exception.extra["errors"])

    def test_status_before_start(self):
        self.assertEqual(self.call("GET", "status"), {"state": "none"})
        with self.assertRaises(server.ApiError):
            self.call("POST", "pause")

    def test_full_copy_then_second_start_and_recover_guard(self):
        with self.assertRaises(server.ApiError):
            self.call("POST", "recover/open")
        self.call("POST", "start", {"source": self.src, "dest": self.dst, "folder": "DR"})
        s = self.wait_done()
        self.assertEqual(s["state"], "done", s)
        self.assertEqual(s["folder"], "DR")
        self.assertTrue(os.path.exists(s["image"]))
        with self.assertRaises(server.ApiError):
            self.call("GET", "recover/list", query={"volume": ["0"]})

    def test_recover_input_checks(self):
        with self.assertRaises(server.ApiError):
            self.call("POST", "recover/copy", {"items": []})
        with self.assertRaises(server.ApiError):
            self.call("POST", "recover/copy", {"items": "x"})


class CloneApiTests(unittest.TestCase):
    def setUp(self):
        self._work = clone_mod.WORK_DIR
        self.sys = Pair(source_size=4 * 1024 * 1024)
        clone_mod.WORK_DIR = os.path.join(self.sys.root, "work")
        self.app = server.App(self.sys)
        self.body = {"source": self.sys.source_file, "dest": self.sys.dest_file}

    def tearDown(self):
        self.app.shutdown()
        clone_mod.WORK_DIR = self._work
        self.sys.cleanup()

    def call(self, route, body, method="POST"):
        return self.app.dispatch(method, route, {}, body)

    def test_plan_summary_lists_what_will_be_erased(self):
        p = self.call("clone/plan", self.body)
        self.assertTrue(p["ok"])
        self.assertEqual(p["dest_contents"][0]["name"], "Old stuff")
        self.assertNotIn("mapfile", p)

    def test_warns_when_the_rescued_copy_is_saved_on_the_target(self):
        self.app.plan = {"image": "/mnt/x/failing.img", "dest_drive": {"path": self.sys.dest_file}}
        warned = " ".join(self.call("clone/plan", self.body)["warnings"])
        self.assertIn("rescued copy", warned)
        self.app.plan = {"image": "/mnt/x/failing.img", "dest_drive": {"path": "/dev/other"}}
        self.assertNotIn("rescued copy", " ".join(self.call("clone/plan", self.body)["warnings"]))
        self.app.plan = {"mode": "clone", "image": self.sys.dest_file, "dest_drive": {"path": self.sys.dest_file}}
        self.assertNotIn("rescued copy", " ".join(self.call("clone/plan", self.body)["warnings"]))

    def test_a_smaller_target_reports_whether_the_files_would_fit(self):
        self.sys.used_bytes = lambda part, scratch: 1024 * 1024
        GIB = 1024**3
        self.sys.tweak = lambda devs: (devs[0].update(size=200 * GIB), devs[1].update(size=100 * GIB))
        p = self.call("clone/plan", self.body)
        self.assertFalse(p["ok"])
        self.assertTrue(p["shrink"]["known"] and p["shrink"]["fits"])

    def test_start_needs_the_typed_confirmation(self):
        for word in ("", "yes", "erase me"):
            with self.assertRaises(server.ApiError) as cm:
                self.call("clone/start", {**self.body, "confirm": word})
            self.assertIn("ERASE", str(cm.exception))
        self.assertEqual(self.call("GET".lower() and "status", {}, "GET"), {"state": "none"})

    def test_failing_source_cannot_start_even_when_confirmed(self):
        self.sys.smart[self.sys.source_file] = {"smart_status": {"passed": False}}
        with self.assertRaises(server.ApiError) as cm:
            self.call("clone/start", {**self.body, "confirm": "ERASE"})
        self.assertEqual(cm.exception.status, 400)

    def test_confirmed_clone_runs_to_the_end_and_cannot_be_opened_as_an_image(self):
        self.call("clone/start", {**self.body, "confirm": "erase"})
        end = time.time() + 60
        while time.time() < end:
            s = self.call("status", {}, "GET")
            if s["state"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(s["state"], "done", s)
        self.assertEqual(s["mode"], "clone")
        with self.assertRaises(server.ApiError):
            self.call("recover/open", {})


class PowerTests(AppCase):
    def setUp(self):
        super().setUp()
        self.sys.power_calls = []
        self.sys.power = lambda action: (self.sys.power_calls.append(action) or (True, ""))

    def tearDown(self):
        os.environ.pop("DRIVE_RESCUE_ALLOW_POWER", None)
        super().tearDown()

    def test_refused_unless_running_from_the_boot_disc(self):
        self.assertFalse(self.call("GET", "system/info")["power"])
        with self.assertRaises(server.ApiError) as cm:
            self.call("POST", "system/power", {"action": "poweroff"})
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(self.sys.power_calls, [])

    def test_allowed_on_the_boot_disc_but_not_during_a_copy(self):
        os.environ["DRIVE_RESCUE_ALLOW_POWER"] = "1"
        self.assertTrue(self.call("GET", "system/info")["power"])
        with self.assertRaises(server.ApiError):
            self.call("POST", "system/power", {"action": "format"})
        self.call("POST", "start", {"source": self.src, "dest": self.dst, "folder": "DR"})
        with self.assertRaises(server.ApiError) as cm:
            self.call("POST", "system/power", {"action": "reboot"})
        self.assertEqual(cm.exception.status, 409)
        self.wait_done()
        self.call("POST", "system/power", {"action": "reboot"})
        self.assertEqual(self.sys.power_calls, ["reboot"])


class HttpTests(AppCase):
    def setUp(self):
        super().setUp()
        self.httpd, self.url = server.serve(self.app, port=0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, path, token=None, host=None, body=None):
        r = urllib.request.Request(self.url.rstrip("/") + path, data=None if body is None else json.dumps(body).encode())
        if token:
            r.add_header("X-Token", token)
        if host:
            r.add_header("Host", host)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page_gets_token(self):
        code, page = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(self.app.token.encode(), page)
        self.assertNotIn(b"__TOKEN__", page.replace(b'const TOKEN = "', b""))

    def test_api_needs_token(self):
        self.assertEqual(self.req("/api/drives")[0], 403)
        self.assertEqual(self.req("/api/drives", token="wrong")[0], 403)
        code, data = self.req("/api/drives", token=self.app.token)
        self.assertEqual(code, 200)
        self.assertIn("drives", json.loads(data))

    def test_bad_host_refused(self):
        self.assertEqual(self.req("/", host="evil.example")[0], 403)
        self.assertEqual(self.req("/api/drives", token=self.app.token, host="evil.example")[0], 403)

    def test_post_and_errors_are_json(self):
        code, data = self.req("/api/plan", token=self.app.token, body={"source": self.src, "dest": self.dst})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(data)["ok"])
        code, data = self.req("/api/start", token=self.app.token, body={"source": self.src, "dest": "/dev/fake-sdc1"})
        self.assertEqual(code, 400)
        self.assertIn("error", json.loads(data))
        self.assertEqual(self.req("/api/zzz", token=self.app.token)[0], 404)
        self.assertEqual(self.req("/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
