"""Tests for the round-3 features: outcome estimate, timing, important-files-first, network folder,
previews, boot check, shrink check and the report."""
import base64
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from drive_rescue import clone, mapfile, partitions, plan as plan_mod, recover, report, server  # noqa: E402
from drive_rescue.system import System, network_mount_attempts  # noqa: E402
from fake import FakeSystem  # noqa: E402
from test_clone import Pair  # noqa: E402
from test_engine import Tweaked, wait_for  # noqa: E402
from test_server import AppCase  # noqa: E402


def mf(*segs):
    m = mapfile.Mapfile()
    m.segments = list(segs)
    return m


class OutlookTests(unittest.TestCase):
    GB = 1000**3

    def test_levels_follow_how_much_could_not_be_read(self):
        total = 100 * self.GB
        clean = mapfile.outlook(mf((0, total, "+")), total)
        self.assertEqual(clean["level"], "excellent")
        tiny = mapfile.outlook(mf((0, total - 4096, "+"), (total - 4096, 4096, "-")), total)
        self.assertEqual(tiny["level"], "excellent")
        good = mapfile.outlook(mf((0, 99.7 * self.GB, "+"), (int(99.7 * self.GB), int(0.3 * self.GB), "*")), total)
        self.assertEqual(good["level"], "good")
        fair = mapfile.outlook(mf((0, 97 * self.GB, "+"), (97 * self.GB, 3 * self.GB, "/")), total)
        self.assertEqual(fair["level"], "fair")
        poor = mapfile.outlook(mf((0, 80 * self.GB, "+"), (80 * self.GB, 20 * self.GB, "-")), total)
        self.assertEqual(poor["level"], "poor")
        self.assertEqual(poor["bad_areas"], 1)
        self.assertEqual(poor["largest_bad"], 20 * self.GB)

    def test_nothing_tried_means_no_estimate(self):
        self.assertIsNone(mapfile.outlook(mf(), 1000))
        self.assertIsNone(mapfile.outlook(mf((0, 1000, "?")), 1000))


class JobFeatureTests(unittest.TestCase):
    """The job with the fake ddrescue: timing, the estimate and 'important files first'."""
    TERMINAL = ("done", "paused", "stopped", "error")

    def setUp(self):
        self._root = plan_mod.MOUNT_ROOT
        self.fs = Tweaked()
        plan_mod.MOUNT_ROOT = os.path.join(self.fs.root, "mnt")
        with open(self.fs.source_file + ".flaky", "w") as fh:
            fh.write("%d %d\n" % (10 * 64 * 1024, 2 * 64 * 1024))

    def tearDown(self):
        plan_mod.MOUNT_ROOT = self._root
        os.environ.pop("FAKE_DELAY", None)
        self.fs.cleanup()

    def job(self, **opts):
        from drive_rescue.job import ImageJob
        o = {"style": "fast", "retries": 3, **opts}
        plan = plan_mod.make_plan(self.fs, self.fs.source_file, "/dev/fake-sdb1", None, o)
        self.assertTrue(plan["ok"], plan)
        return ImageJob(self.fs, plan, o)

    def test_pauses_after_the_quick_copy_when_asked_then_finishes_on_resume(self):
        os.environ["FAKE_DELAY"] = "0.002"
        job = self.job(pause_after_quick=True)
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "paused")
        snap = job.snapshot()
        self.assertIn("important files", snap["message"])
        self.assertTrue(snap["quick_done"])
        self.assertGreater(snap["skipped"], 0)
        self.assertIsNotNone(snap["outlook"])
        job.resume()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        self.assertEqual(job.snapshot()["skipped"], 0)

    def test_without_the_option_it_runs_straight_through(self):
        os.environ["FAKE_DELAY"] = "0.002"
        job = self.job()
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")

    def test_no_pause_when_the_quick_pass_left_nothing_behind(self):
        os.remove(self.fs.source_file + ".flaky")
        os.environ["FAKE_DELAY"] = "0.002"
        job = self.job(pause_after_quick=True)
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")

    def test_elapsed_time_is_reported(self):
        os.environ["FAKE_DELAY"] = "0.002"
        job = self.job()
        self.assertEqual(job.snapshot()["elapsed_text"], "-")
        job.start()
        wait_for(job, self.TERMINAL)
        snap = job.snapshot()
        self.assertGreaterEqual(snap["elapsed"], 0)
        self.assertNotEqual(snap["elapsed_text"], "-")
        self.assertEqual(snap["speed_note"], "")


class BootAndShrinkTests(unittest.TestCase):
    def gpt(self, *types):
        return {"kind": "gpt", "parts": [{"slot": i, "type": t, "start": 0, "end": 1} for i, t in enumerate(types)]}

    def test_gpt_with_an_efi_partition_boots_in_uefi_mode(self):
        r = clone.boot_check(self.gpt(partitions.GPT_ESP, "x"), "uefi")
        self.assertEqual((r["mode"], r["level"]), ("uefi", "ok"))
        self.assertIn("BIOS mode", clone.boot_check(self.gpt(partitions.GPT_ESP), "bios")["text"])

    def test_gpt_with_a_bios_boot_area_boots_in_legacy_mode(self):
        self.assertEqual(clone.boot_check(self.gpt(partitions.GPT_BIOS_BOOT), "uefi")["mode"], "legacy")

    def test_gpt_without_either_is_a_data_drive(self):
        self.assertEqual(clone.boot_check(self.gpt("x"))["mode"], "data")

    def test_mbr_active_partition_is_legacy_and_warns_on_a_uefi_computer(self):
        layout = {"kind": "mbr", "parts": [{"type": 7, "active": True}]}
        self.assertEqual(clone.boot_check(layout, "bios")["level"], "ok")
        warn = clone.boot_check(layout, "uefi")
        self.assertEqual((warn["mode"], warn["level"]), ("legacy", "warn"))
        self.assertEqual(clone.boot_check({"kind": "mbr", "parts": [{"type": 7, "active": False}]})["mode"], "data")

    def test_no_table_is_unknown(self):
        self.assertEqual(clone.boot_check({"kind": "none"})["mode"], "unknown")


class ShrinkTests(unittest.TestCase):
    def setUp(self):
        self._work = clone.WORK_DIR
        self.sysm = Pair(source_size=4 * 1024 * 1024, dest_factor=1)
        clone.WORK_DIR = os.path.join(self.sysm.root, "work")
        self.used = None
        self.sysm.used_bytes = lambda part, scratch: self.used
        GIB = 1024**3

        def sizes(devs):  # a 200 GiB original and a 100 GiB target
            devs[0]["size"], devs[1]["size"] = 200 * GIB, 100 * GIB
        self.sysm.tweak = sizes

    def tearDown(self):
        clone.WORK_DIR = self._work
        self.sysm.cleanup()

    def plan(self):
        return clone.make_clone_plan(self.sysm, self.sysm.source_file, self.sysm.dest_file)

    def test_smaller_target_is_still_refused_but_says_whether_the_files_would_fit(self):
        self.used = 1024**3
        p = self.plan()
        self.assertFalse(p["ok"])
        self.assertTrue(p["shrink"]["fits"])
        self.assertIn("shrink the main partition", " ".join(p["errors"]))

    def test_too_much_data_for_the_smaller_target(self):
        self.used = 150 * 1024**3
        p = self.plan()
        self.assertFalse(p["shrink"]["fits"])
        self.assertIn("bigger target", " ".join(p["errors"]))

    def test_unmeasurable_source_adds_no_extra_advice(self):
        self.used = None
        p = self.plan()
        self.assertFalse(p["shrink"]["known"])
        self.assertNotIn("shrink the main partition", " ".join(p["errors"]))
        self.assertTrue(any("smaller than the drive" in e for e in p["errors"]))

    def test_the_plan_carries_the_boot_check(self):
        sysm = Pair(source_size=4 * 1024 * 1024)
        try:
            clone.WORK_DIR = os.path.join(sysm.root, "work")
            p = clone.make_clone_plan(sysm, sysm.source_file, sysm.dest_file)
            self.assertIn("boot", p["layout"])
        finally:
            sysm.cleanup()


class NetworkTests(unittest.TestCase):
    def test_mount_commands(self):
        smb = network_mount_attempts("smb", "nas.local", "Backup", "/mnt/n", "/run/cred")
        self.assertEqual(smb[0][:5], ["mount", "-t", "cifs", "//nas.local/Backup", "/mnt/n"])
        self.assertIn("credentials=/run/cred", smb[0][-1])
        self.assertNotIn("password", " ".join(" ".join(c) for c in smb))
        self.assertIn("guest", network_mount_attempts("smb", "h", "s", "/m")[0][-1])
        nfs = network_mount_attempts("nfs", "10.0.0.2", "/vol/bk", "/mnt/n")
        self.assertEqual(nfs[0][-2], "10.0.0.2:/vol/bk")
        with self.assertRaises(ValueError):
            network_mount_attempts("ftp", "h", "s", "/m")


class NetworkApiTests(AppCase):
    def setUp(self):
        super().setUp()
        self.mounted = []
        netdir = os.path.join(self.sys.root, "netshare")
        os.makedirs(netdir)

        def fake_mount(kind, address, share, mountpoint, user="", password=""):
            self.mounted.append((kind, address, share, user, password))
            os.makedirs(os.path.dirname(mountpoint), exist_ok=True)
            if os.path.lexists(mountpoint):
                os.remove(mountpoint) if os.path.islink(mountpoint) else None
            if not os.path.exists(mountpoint):
                os.symlink(netdir, mountpoint)
            return True, ""
        self.sys.mount_network = fake_mount
        self.sys.umount = lambda mp: (True, "")

    def connect(self, **kw):
        body = {"kind": "smb", "address": "nas.local", "share": "Backup", "user": "me", "password": "pw", **kw}
        return self.call("POST", "network/connect", body)

    def test_input_is_checked(self):
        for bad in ({"kind": "ftp"}, {"address": "bad host!"}, {"address": "-x"}, {"share": "a/b"},
                    {"kind": "nfs", "share": "relative"}, {"password": "a\nusername=root"}, {"user": "x" * 200}):
            with self.assertRaises(server.ApiError, msg=str(bad)):
                self.connect(**bad)
        self.assertEqual(self.mounted, [])

    def test_connect_then_plan_to_the_network(self):
        r = self.connect()
        self.assertTrue(r["ok"])
        self.assertEqual(self.mounted[0][:3], ("smb", "nas.local", "Backup"))
        p = self.call("POST", "plan", {"source": self.src, "dest": "network", "folder": "DR"})
        self.assertTrue(p["ok"], p)
        self.assertIn("network", " ".join(p["warnings"]))

    def test_network_destination_needs_a_connection_first(self):
        with self.assertRaises(server.ApiError):
            self.call("POST", "plan", {"source": self.src, "dest": "network"})

    def test_a_copy_to_the_network_runs_and_disconnect_forgets_it(self):
        os.environ["FAKE_DELAY"] = "0.002"
        try:
            self.connect()
            self.call("POST", "start", {"source": self.src, "dest": "network", "folder": "DR"})
            self.wait_done()
            self.assertTrue(os.path.exists(os.path.join(self.sys.root, "netshare", "DR")))
            self.call("POST", "network/disconnect")
            with self.assertRaises(server.ApiError):
                self.call("POST", "plan", {"source": self.src, "dest": "network"})
        finally:
            os.environ.pop("FAKE_DELAY", None)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="preview-")
        self.rec = recover.Recovery(FakeSystem(), "x.img", os.path.join(self.tmp, "out"))
        self.root = os.path.join(self.tmp, "vol")
        os.makedirs(self.root)
        self.rec.volumes = [{"id": "0", "name": "V", "fstype": "ext4", "size_text": "", "mountpoint": self.root}]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, name, data):
        with open(os.path.join(self.root, name), "wb") as fh:
            fh.write(data)

    PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

    def test_picture_text_and_unknown(self):
        self.put("a.png", self.PNG)
        self.put("n.txt", b"hello\nworld")
        self.put("x.bin", b"\0\1\2")
        img = self.rec.preview("0", "a.png")
        self.assertEqual((img["kind"], img["mime"]), ("image", "image/png"))
        self.assertEqual(base64.b64decode(img["data"]), self.PNG)
        self.assertEqual(self.rec.preview("0", "n.txt")["text"], "hello\nworld")
        self.assertEqual(self.rec.preview("0", "x.bin")["kind"], "none")

    def test_a_damaged_picture_is_not_sent_and_a_big_one_is_skipped(self):
        self.put("bad.jpg", b"not really a jpeg")
        self.assertIn("damaged", self.rec.preview("0", "bad.jpg")["message"])
        self.put("huge.png", self.PNG + b"\0" * recover.PREVIEW_IMAGE_MAX)
        self.assertIn("too large", self.rec.preview("0", "huge.png")["message"])

    def test_long_text_is_cut_short(self):
        self.put("big.txt", b"a" * (recover.PREVIEW_TEXT_BYTES * 3))
        r = self.rec.preview("0", "big.txt")
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["text"]), recover.PREVIEW_TEXT_BYTES)

    def test_links_and_escapes_are_refused(self):
        os.symlink("/etc/hostname", os.path.join(self.root, "link.txt"))
        with self.assertRaises(recover.RecoverError):
            self.rec.preview("0", "link.txt")
        with self.assertRaises(recover.RecoverError):
            self.rec.preview("0", "../../etc/passwd")
        with self.assertRaises(recover.RecoverError):
            self.rec.preview("0", "")
        with self.assertRaises(recover.RecoverError):
            self.rec.preview("9", "a.txt")


class ReportTests(unittest.TestCase):
    PLAN = {"source": {"name": "Old HDD", "size_text": "500 GB", "serial": "S123", "kind": "Hard drive", "transport": "SATA"},
            "dest_drive": {"name": "New HDD", "size_text": "2 TB"}, "target_dir": "/mnt/x/DriveRescue",
            "image": "/mnt/x/DriveRescue/old.img"}
    SNAP = {"state": "done", "percent": 99.5, "finished_text": "497 GB", "size_text": "500 GB", "skipped_text": "2 GB",
            "elapsed_text": "3 h 10 min", "complete": False,
            "outlook": {"title": "Very good", "text": "Most is safe.", "bad_areas": 4, "largest_bad": 1_000_000}}

    def test_rescue_report(self):
        text = report.build_report(self.PLAN, self.SNAP, {"label": "Failing", "details": [{"k": "Temp", "v": "40"}]},
                                   {"state": "done", "kind": "copy", "copied": 12, "bytes": 5, "bytes_text": "5 B",
                                    "failed_count": 1, "failed": [{"path": "a/b.doc", "reason": "unreadable"}],
                                    "out_dir": "/mnt/x/recovered"}, when="Today")
        for want in ("Old HDD", "S123", "99.5% copied", "Very good", "12 files recovered", "a/b.doc", "never written to"):
            self.assertIn(want, text)

    def test_clone_report_says_the_target_was_replaced(self):
        text = report.build_report(self.PLAN, {**self.SNAP, "mode": "clone"}, None, None, when="Today")
        self.assertIn("Target drive: New HDD", text)
        self.assertIn("replaced", text)


class ServerFeatureTests(AppCase):
    def test_report_needs_a_job_then_is_saved_next_to_the_image(self):
        with self.assertRaises(server.ApiError):
            self.call("GET", "report")
        os.environ["FAKE_DELAY"] = "0.002"
        try:
            self.call("POST", "start", {"source": self.src, "dest": self.dst, "folder": "DR"})
            self.wait_done()
            r = self.call("GET", "report")
        finally:
            os.environ.pop("FAKE_DELAY", None)
        self.assertIn("DRIVE RESCUE REPORT", r["text"])
        self.assertTrue(r["saved_to"].endswith(".report.txt") and os.path.exists(r["saved_to"]))

    def test_preview_needs_an_open_copy(self):
        with self.assertRaises(server.ApiError):
            self.call("GET", "recover/preview", query={"volume": ["0"], "rel": ["a"]})


if __name__ == "__main__":
    unittest.main()
