"""The USB bundle: it builds, contains what it should, and the unpacked copy really starts."""
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "usb"))

import build  # noqa: E402
from drive_rescue import preflight  # noqa: E402


class PreflightTests(unittest.TestCase):
    def test_everything_present(self):
        r = preflight.run_checks(which=lambda c: "/usr/bin/" + c, euid=0)
        self.assertTrue(r["ok"])

    def test_missing_ddrescue_blocks_but_missing_extras_do_not(self):
        have = {"losetup", "blockdev", "mount", "umount"}
        r = preflight.run_checks(which=lambda c: "/x/" + c if c in have else None, euid=0)
        self.assertFalse(r["ok"])
        self.assertFalse(next(i for i in r["items"] if i["key"] == "ddrescue")["ok"])
        have |= {"ddrescue"}
        r = preflight.run_checks(which=lambda c: "/x/" + c if c in have else None, euid=0)
        self.assertTrue(r["ok"])  # smartctl, photorec, resizers and a browser are optional
        self.assertFalse(next(i for i in r["items"] if i["key"] == "smartctl")["ok"])

    def test_not_root_blocks(self):
        self.assertFalse(preflight.run_checks(which=lambda c: "/x/" + c, euid=1000)["ok"])


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bundle-")
        cls.zip = build.build(os.path.join(cls.tmp, "b.zip"))
        with zipfile.ZipFile(cls.zip) as zf:
            cls.names = zf.namelist()
            zf.extractall(cls.tmp + "/stick")

    def test_contents(self):
        for need in ("autorun/autorun", "drive-rescue/start.sh", "drive-rescue/README.txt",
                     "drive-rescue/drive_rescue/ui/index.html", "drive-rescue/drive_rescue/__main__.py",
                     "drive-rescue/drive_rescue/clone.py", "drive-rescue/drive_rescue/partitions.py"):
            self.assertIn(need, self.names)
        self.assertFalse([n for n in self.names if "__pycache__" in n or n.endswith(".pyc") or "/tests/" in n or "fake" in n])

    def test_scripts_are_valid_and_unix_line_endings(self):
        for rel in ("autorun/autorun", "drive-rescue/start.sh"):
            path = os.path.join(self.tmp, "stick", rel)
            self.assertEqual(subprocess.run(["sh", "-n", path]).returncode, 0, rel)
            self.assertNotIn(b"\r", open(path, "rb").read())

    def test_the_unpacked_copy_starts_and_serves_the_screens(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.Popen([sys.executable, "-u", "-m", "drive_rescue", "--no-browser", "--port", "0"],
                                cwd=os.path.join(self.tmp, "stick", "drive-rescue"), env={**env, "PYTHONPATH": os.path.join(self.tmp, "stick", "drive-rescue")},
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            line = proc.stdout.readline()
            url = re.search(r"http://\S+", line).group(0)
            page = urllib.request.urlopen(url, timeout=10).read().decode()
            self.assertIn("Drive Rescue", page)
            self.assertNotIn("__TOKEN__", page.split("const TOKEN")[1].split(";")[0])
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    @unittest.skipUnless(os.geteuid() == 0, "start.sh must run as root, and re-launches itself with sudo otherwise")
    def test_start_script_check_mode_reports_and_exits(self):
        start = os.path.join(self.tmp, "stick", "drive-rescue", "start.sh")
        r = subprocess.run(["sh", start, "--check"], capture_output=True, text=True, timeout=60)
        self.assertIn("Checking this system", r.stdout)
        self.assertIn("ddrescue", r.stdout)
        self.assertEqual(r.returncode, 0 if "[MISSING]" not in r.stdout else 1)
        # the stick must never be written to
        self.assertFalse([n for _d, _s, fs in os.walk(os.path.join(self.tmp, "stick")) for n in fs if n.endswith(".pyc")])


if __name__ == "__main__":
    unittest.main()
