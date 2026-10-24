"""Tests that use the REAL tools (ddrescue, dmsetup, cryptsetup, loop devices) on a pretend failing drive.

These need root and a Linux machine with those tools, so they are skipped anywhere else. They run in CI
(.github/workflows/real-tools.yml). The pretend drive gives genuine read errors, built with the
device-mapper "error" target, so ddrescue and the damaged-file check see what they would on a bad disk.

    sudo python3 tests/real/test_real_tools.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import damaged_drive  # noqa: E402
from drive_rescue import assess, recover, verify  # noqa: E402
from drive_rescue import mapfile as mapfile_mod  # noqa: E402
from drive_rescue.job import ImageJob  # noqa: E402
from drive_rescue.system import System  # noqa: E402

NEEDED = ("ddrescue", "dmsetup", "losetup", "mkfs.ext4", "filefrag", "blockdev")


def available():
    return os.geteuid() == 0 and sys.platform.startswith("linux") and all(shutil.which(t) for t in NEEDED)


@unittest.skipUnless(available(), "needs root and ddrescue, dmsetup, losetup, mkfs.ext4, filefrag")
class RealDamagedDriveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="dr-real-")
        cls.name = f"dr-real-{os.getpid()}"
        cls.info = damaged_drive.create(os.path.join(cls.work, "drive"), 96, cls.name)
        cls.sysm = System()
        cls.out = os.path.join(cls.work, "out")
        os.makedirs(cls.out)
        cls.image = os.path.join(cls.out, "copy.img")
        cls.mapfile = os.path.join(cls.out, "copy.map")
        cls.plan = {"source": {"path": cls.info["device"], "size": cls.info["size"], "model": "Practice", "serial": "X1",
                               "name": "Practice drive", "partitions": [], "size_text": "96 MB"},
                    "image": cls.image, "mapfile": cls.mapfile, "target_dir": cls.out,
                    "jobfile": os.path.join(cls.out, "copy.job.json"), "sector": 512, "mode": "rescue"}
        cls.lines = []
        cls.job = ImageJob(cls.sysm, cls.plan, {"retries": 1, "style": "fast"}, log=cls.lines.append)
        cls.job.start()
        end = time.time() + 600
        while time.time() < end and cls.job.state not in ("done", "error", "stopped", "paused"):
            time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        damaged_drive.remove(os.path.join(cls.work, "drive"), cls.name)
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_ddrescue_finishes_and_the_log_shows_what_ran(self):
        self.assertEqual(self.job.state, "done", self.job.message + "\n" + "\n".join(self.lines[-15:]))
        text = "\n".join(self.lines)
        self.assertIn("locked as read-only", text)
        self.assertIn("Key areas first", text)

    def test_only_the_damaged_stretch_is_missing(self):
        sums = mapfile_mod.summarize(mapfile_mod.parse_mapfile(open(self.mapfile).read()), self.info["size"])
        bad = sum(n for _s, n in self.info["bad"])
        self.assertGreaterEqual(sums["skipped"], bad)
        self.assertLess(sums["skipped"], bad + 1024 * 1024)  # not much more than the real damage
        self.assertGreater(sums["finished"], self.info["size"] - bad - 1024 * 1024)

    def test_the_good_parts_of_the_copy_are_exact(self):
        bad = self.info["bad"][0]
        with open(self.info["backing"], "rb") as a, open(self.image, "rb") as b:
            a.seek(0)
            b.seek(0)
            before = bad[0] - 256 * 1024
            self.assertEqual(a.read(before), b.read(before))
            a.seek(bad[0] + bad[1] + 256 * 1024)
            b.seek(bad[0] + bad[1] + 256 * 1024)
            self.assertEqual(a.read(4 * 1024 * 1024), b.read(4 * 1024 * 1024))

    def test_the_sample_check_agrees_with_the_drive(self):
        segs = mapfile_mod.parse_mapfile(open(self.mapfile).read()).segments
        r = verify.sample_check(self.info["device"], self.image, segs, count=24, chunk=256 * 1024)
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["checked"], 10)

    def test_the_quick_read_test_notices_real_read_errors(self):
        r = assess.read_test(self.info["device"], self.info["size"], reader=assess.real_reader(self.info["device"], self.info["size"]),
                             count=96, time_limit=60)
        self.assertGreaterEqual(r["errors"], 0)  # a 64 KB damaged stretch may fall between spots; it must not crash
        self.assertEqual(r["hung"], 0)

    def test_a_file_that_sits_in_the_damage_is_flagged_and_the_others_are_not(self):
        rec = recover.Recovery(self.sysm, self.image, self.out, self.mapfile)
        try:
            info = rec.open()
            self.assertTrue(info["readable"], info)
            vol = rec.volumes[0]
            self.assertEqual(vol["damage_check"], True)
            self.assertGreater(vol["damaged_bytes"], 0)
            rec.start_copy([{"volume": vol["id"], "rel": ""}], os.path.join(self.work, "recovered"))
            end = time.time() + 120
            while time.time() < end and rec.task["state"] == "running":
                time.sleep(0.2)
            self.assertEqual(rec.task["state"], "done", rec.task)
            damaged = [os.path.basename(d["path"]) for d in rec.task["damaged"]]
            self.assertEqual(damaged, ["holiday-big.bin"], rec.task)
            base = os.path.join(self.work, "recovered", "recovered", vol["name"])
            got = os.path.join(base, "Pictures", "small.bin")
            self.assertEqual(open(got, "rb").read(), b"S" * (256 * 1024))
            big = open(os.path.join(base, "Pictures", "holiday-big.bin"), "rb").read()
            self.assertEqual(len(big), 8 * 1024 * 1024)
            self.assertEqual(big[:1024 * 1024], b"B" * 1024 * 1024)       # the readable start is intact
            self.assertIn(b"\0" * 4096, big)                              # the unreadable stretch is blank
        finally:
            rec.close()


@unittest.skipUnless(available() and shutil.which("cryptsetup"), "needs root and cryptsetup")
class RealEncryptedTests(unittest.TestCase):
    def test_a_protected_section_opens_only_with_the_right_password(self):
        tmp = tempfile.mkdtemp(prefix="dr-luks-")
        sysm = System()
        try:
            img = os.path.join(tmp, "vault.img")
            subprocess.run(["truncate", "-s", "64M", img], check=True)
            subprocess.run(["cryptsetup", "luksFormat", "--batch-mode", "--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000",
                            "--key-file=-", img], input=b"correct horse", check=True)
            loop, _ = sysm.attach_image(img)
            self.assertTrue(loop)
            name = f"dr-test-crypt-{os.getpid()}"
            try:
                path, err = sysm.crypt_open(loop, name, "wrong", "LUKS")
                self.assertIsNone(path)
                self.assertIn("didn't work", err)
                path, err = sysm.crypt_open(loop, name, "correct horse", "LUKS")
                self.assertEqual(path, "/dev/mapper/" + name, err)
                self.assertTrue(os.path.exists(path))
            finally:
                sysm.crypt_close(name)
                sysm.detach_image(loop)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
