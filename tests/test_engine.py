"""Tests for the copy engine: drive listing, the safety gate, ddrescue plumbing and the job.

Nothing here touches a real disk. FakeSystem (tests/fake.py) stands in for the machine, and
tests/fake_ddrescue.py stands in for ddrescue, working on ordinary files.
"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from drive_rescue import ddrescue, drives, mapfile, plan as plan_mod, util  # noqa: E402
from drive_rescue.job import ImageJob, JobError  # noqa: E402
from fake import SHIM, FakeSystem  # noqa: E402

BLOCK = 64 * 1024


class Tweaked(FakeSystem):
    """FakeSystem whose drive list can be altered per test."""

    def __init__(self, tweak=None, free=None, **kw):
        super().__init__(**kw)
        self.tweak, self.free = tweak, free
        os.chmod(SHIM, 0o755)

    def lsblk(self):
        devs = super().lsblk()
        if self.tweak:
            self.tweak(devs)
        return devs

    def free_bytes(self, path):
        return self.free if self.free is not None else super().free_bytes(path)


class Base(unittest.TestCase):
    def setUp(self):
        self._root = plan_mod.MOUNT_ROOT
        self.systems = []

    def tearDown(self):
        plan_mod.MOUNT_ROOT = self._root
        for s in self.systems:
            s.cleanup()

    def system(self, **kw):
        s = Tweaked(**kw)
        plan_mod.MOUNT_ROOT = os.path.join(s.root, "mnt")  # never create /mnt/... for real
        self.systems.append(s)
        return s


# ------------------------------------------------------------------ pure helpers

MAPFILE = """# Mapfile. Created by GNU ddrescue version 1.27
# Command line: ddrescue -n /dev/sda image.img image.map
# current_pos  current_status  current_pass
0x00060000     ?               1
#      pos        size  status
0x00000000  0x00040000  +
0x00040000  0x00010000  *
0x00050000  0x00010000  +
0x00060000  0x00020000  ?
"""

STATUS_127 = (
    "     ipos:    1560 MB, non-trimmed:        0 B,  current rate:   11796 kB/s\n"
    "     opos:    1560 MB, non-scraped:        0 B,  average rate:   14894 kB/s\n"
    "non-tried:    2439 MB,  bad-sector:        0 B,    error rate:       0 B/s\n"
    "  rescued:    1560 MB,   bad areas:        0,        run time:      1m 44s\n"
    "pct rescued:   38.99%, read errors:        0,  remaining time:      3m 11s\n"
)
STATUS_OLD = "rescued:   760 GB,  errsize:   14336 kB,  current rate:   92 MB/s\n"


class HelperTests(unittest.TestCase):
    def test_mapfile_parse_summary_and_cells(self):
        mf = mapfile.parse_mapfile(MAPFILE)
        self.assertEqual(mf.current_pass, 1)
        self.assertEqual(len(mf.segments), 4)
        s = mapfile.summarize(mf, 0x80000)
        self.assertEqual((s["finished"], s["skipped"], s["untried"]), (0x50000, 0x10000, 0x20000))
        self.assertEqual(mapfile.build_cells(mf, 0x80000, 8),
                         ["done"] * 4 + ["skipped", "done", "waiting", "waiting"])

    def test_mapfile_tolerates_a_half_written_file(self):
        mf = mapfile.parse_mapfile("# x\n0x0 ? 1\n0x0 0x100 +\n0x100 0x2")  # cut off mid-line
        self.assertEqual(mapfile.summarize(mf, 0x100)["finished"], 0x100)

    def test_ddrescue_commands(self):
        q = ddrescue.build_command("ddr", "/dev/sda", "a.img", "a.map", "quick", {})
        self.assertEqual(q, ["ddr", "-d", "-p", "-c", "32", "-n", "/dev/sda", "a.img", "a.map"])
        r = ddrescue.build_command("ddr", "/dev/sda", "a.img", "a.map", "retry",
                                   {"style": "fast", "retries": 5, "direction": "backward"})
        self.assertEqual(r, ["ddr", "-d", "-p", "-r", "5", "-R", "/dev/sda", "a.img", "a.map"])
        with self.assertRaises(ValueError):
            ddrescue.build_command("ddr", "x", "y", "z", "nonsense", {})

    def test_drives_with_4k_sectors_are_told_so(self):
        q = ddrescue.build_command("ddr", "/dev/sda", "a.img", "a.map", "quick", {"style": "fast"}, sector=4096)
        self.assertEqual(q, ["ddr", "-d", "-p", "-b", "4096", "-n", "/dev/sda", "a.img", "a.map"])
        plain = ddrescue.build_command("ddr", "/dev/sda", "a.img", "a.map", "quick", {"style": "fast"}, sector=512)
        self.assertNotIn("-b", plain)

    def test_quick_pass_skips_trimming_only_when_ddrescue_supports_it(self):
        on = ddrescue.build_command("ddr", "x", "y", "z", "quick", {"style": "fast", "no_trim": True})
        self.assertIn("--no-trim", on)
        retry = ddrescue.build_command("ddr", "x", "y", "z", "retry", {"style": "fast", "no_trim": True})
        self.assertNotIn("--no-trim", retry)  # the retry pass is where trimming happens
        self.assertNotIn("--no-trim", ddrescue.build_command("ddr", "x", "y", "z", "quick", {"style": "fast"}))

    def test_exit_codes_are_explained(self):
        self.assertIn("drives or files", ddrescue.explain_exit(1))
        self.assertEqual(ddrescue.explain_exit(99), "")

    def test_the_failing_drive_is_only_ever_the_input(self):
        for phase in ("quick", "retry"):
            cmd = ddrescue.build_command("ddr", "/dev/sda", "/mnt/b/a.img", "/mnt/b/a.map", phase, {})
            self.assertEqual(cmd[-3:], ["/dev/sda", "/mnt/b/a.img", "/mnt/b/a.map"])
            self.assertNotIn("-f", cmd)  # never forces a write onto a device

    def test_status_parsing(self):
        r = ddrescue.parse_status(STATUS_127)
        self.assertEqual((r["rescued"], r["rate"], r["errors"]), (1560 * 10**6, 11796000, 0))
        o = ddrescue.parse_status(STATUS_OLD)
        self.assertEqual((o["rescued"], o["errsize"], o["rate"]), (760 * 10**9, 14336000, 92 * 10**6))
        self.assertEqual(ddrescue.parse_status("\x1b[1A\rnothing useful"), {})

    def test_util(self):
        self.assertEqual(util.human_size(2 * 10**12), "2 TB")
        self.assertEqual(util.human_size(512 * 10**9), "512 GB")
        self.assertEqual(util.human_duration(5 * 3600 + 20 * 60), "5 h 20 m")
        self.assertEqual(util.parse_size("560 GB"), 560 * 10**9)
        self.assertIsNone(util.parse_size("38.99%"))


# ------------------------------------------------------------------------ drives


class DriveTests(Base):
    def test_listing(self):
        d = {x["path"]: x for x in drives.list_drives(self.system())}
        self.assertEqual(len(d), 3)  # the loop device is left out
        failing = d[next(p for p in d if p.endswith("failing.disk"))]
        self.assertEqual(failing["kind"], "Hard drive")
        self.assertEqual(failing["transport"], "SATA")
        self.assertFalse(failing["is_boot"])
        self.assertTrue(d["/dev/fake-sdc"]["is_boot"])
        self.assertEqual(d["/dev/fake-sdc"]["kind"], "USB stick")
        part = d["/dev/fake-sdb"]["partitions"][0]
        self.assertTrue(part["dest_ok"])
        self.assertEqual(part["size_text"], "4 TB")

    def test_rescue_stick_is_recognised_by_label_alone(self):
        def tweak(devs):
            devs[2]["children"][0]["mountpoints"] = []
        d = {x["path"]: x for x in drives.list_drives(self.system(tweak=tweak))}
        self.assertTrue(d["/dev/fake-sdc"]["is_boot"])

    def test_destination_filesystems(self):
        for fs, ok in (("ntfs", True), ("exfat", True), ("ext4", True), ("vfat", False),
                       ("BitLocker", False), ("", False)):
            d = {x["path"]: x for x in drives.list_drives(self.system(dest_fs=fs))}
            self.assertEqual(d["/dev/fake-sdb"]["partitions"][0]["dest_ok"], ok, fs)

    def test_health(self):
        s = self.system()
        h = drives.check_health(s, s.source_file)
        self.assertEqual(h["status"], "failing")
        self.assertIn("1,204", [x["v"] for x in h["details"]])
        s.smart[s.source_file] = {"smart_status": {"passed": True}}
        self.assertEqual(drives.check_health(s, s.source_file)["status"], "healthy")
        s.smart[s.source_file] = {"smart_status": {"passed": True},
                                  "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 8}}]}}
        self.assertEqual(drives.check_health(s, s.source_file)["status"], "warning")
        s.smart.clear()
        self.assertEqual(drives.check_health(s, s.source_file)["status"], "unknown")


# --------------------------------------------------------------------- the gate


class PlanTests(Base):
    def make(self, system=None, dest="/dev/fake-sdb1", source=None, folder=None, **kw):
        system = system or self.system()
        return system, plan_mod.make_plan(system, source or system.source_file, dest, folder, **kw)

    def assertRefused(self, result, text):
        self.assertFalse(result["ok"], result)
        self.assertTrue(any(text.lower() in e.lower() for e in result["errors"]), result["errors"])

    def test_good_plan_saves_an_image_and_erases_nothing(self):
        s, r = self.make()
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["image"].endswith(".img"))
        self.assertTrue(r["image"].startswith(s.dest_mount))
        self.assertTrue(r["mapfile"].startswith(s.dest_mount))
        self.assertEqual(os.path.basename(r["target_dir"]), "DriveRescue")
        self.assertFalse(r["resume"])
        self.assertFalse([c for c in s.calls if c[0] in ("mount", "umount", "setro")])  # a plan only looks

    def test_cannot_save_onto_the_failing_drive(self):
        def tweak(devs):
            devs[0]["children"] = [{"name": "sda1", "path": "/dev/fake-sda1", "type": "part", "size": 5000000,
                                    "fstype": "ntfs", "label": "Old", "mountpoints": []}]
        _, r = self.make(system=self.system(tweak=tweak), dest="/dev/fake-sda1")
        self.assertRefused(r, "failing drive itself")

    def test_rescue_usb_refused_both_ways(self):
        self.assertRefused(self.make(dest="/dev/fake-sdc1")[1], "rescue usb")
        s = self.system()
        self.assertRefused(self.make(system=s, source="/dev/fake-sdc")[1], "rescue usb")

    def test_unknown_drives(self):
        self.assertRefused(self.make(dest="/dev/nope1")[1], "not found")
        self.assertRefused(self.make(source="/dev/nope")[1], "not found")

    def test_fat32(self):
        _, small = self.make(system=self.system(dest_fs="vfat"))
        self.assertTrue(small["ok"])
        self.assertTrue(any("FAT32" in w for w in small["warnings"]))

        def huge(devs):
            devs[0]["size"] = 5 * 10**12
        _, big = self.make(system=self.system(dest_fs="vfat", tweak=huge))
        self.assertRefused(big, "can't be used")

    def test_encrypted_or_unknown_filesystem(self):
        self.assertRefused(self.make(system=self.system(dest_fs="BitLocker"))[1], "can't be used")
        self.assertRefused(self.make(system=self.system(dest_fs=""))[1], "can't be used")

    def test_not_enough_room(self):
        self.assertRefused(self.make(system=self.system(free=1000))[1], "not enough room")

    def test_folder_names_cannot_escape(self):
        s, r = self.make(folder="../../etc")
        self.assertTrue(r["ok"])
        self.assertEqual(r["target_dir"], os.path.join(s.dest_mount, "etc"))
        self.assertEqual(plan_mod.clean_folder("a/b\\c"), "abc")
        self.assertEqual(plan_mod.clean_folder("..."), "DriveRescue")

    def test_never_overwrites_an_unknown_file(self):
        s, r = self.make()
        os.makedirs(r["target_dir"])
        open(r["image"], "wb").close()
        self.assertRefused(self.make(system=s)[1], "no progress record")

    def test_resume_is_recognised_and_needs_less_room(self):
        s, r = self.make()
        os.makedirs(r["target_dir"])
        with open(r["image"], "wb") as fh:
            fh.truncate(4 * 1024 * 1024)
        open(r["mapfile"], "w").close()
        _, again = self.make(system=s)
        self.assertTrue(again["ok"] and again["resume"], again)
        self.assertLess(again["need"], r["need"])

    def test_a_folder_holding_a_different_drive_is_refused(self):
        s, r = self.make()
        os.makedirs(r["target_dir"])
        open(r["image"], "wb").close()
        open(r["mapfile"], "w").close()
        with open(r["jobfile"], "w") as fh:
            json.dump({"source": {"serial": "SOMEONE-ELSE", "size": s.source_size if hasattr(s, "source_size") else 1}}, fh)
        self.assertRefused(self.make(system=s)[1], "different drive")

    def test_unmounted_destination_is_opened_for_writing(self):
        def tweak(devs):
            devs[1]["children"][0]["mountpoints"] = []
        s, r = self.make(system=self.system(tweak=tweak))
        self.assertTrue(r["ok"], r)
        mounts = [c for c in s.calls if c[0] == "mount"]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0][1], "/dev/fake-sdb1")
        self.assertFalse(mounts[0][4], "the healthy drive must be mounted writable")

    def test_windows_hibernation_is_explained(self):
        def tweak(devs):
            devs[1]["children"][0]["mountpoints"] = []

        class Refuses(Tweaked):
            def mount(self, *a, **k):
                return False, "Windows is hibernated, refused to mount."
        s = Refuses(tweak=tweak)
        plan_mod.MOUNT_ROOT = os.path.join(s.root, "mnt")
        self.systems.append(s)
        r = plan_mod.make_plan(s, s.source_file, "/dev/fake-sdb1", None)
        self.assertRefused(r, "shut down")

    def test_mounted_source_warns(self):
        def tweak(devs):
            devs[0]["children"] = [{"name": "sda1", "path": "/dev/fake-sda1", "type": "part", "size": 5000000,
                                    "fstype": "ntfs", "label": "Old", "mountpoints": ["/mnt/old"]}]
        _, r = self.make(system=self.system(tweak=tweak))
        self.assertTrue(r["ok"])
        self.assertTrue(any("released" in w for w in r["warnings"]))


# -------------------------------------------------------------------------- job


def wait_for(job, states, timeout=30):
    end = time.time() + timeout
    while time.time() < end and job.state not in states:
        time.sleep(0.05)
    return job.state


class JobTests(Base):
    TERMINAL = ("done", "paused", "stopped", "error")

    def setUp(self):
        super().setUp()
        self.fs = self.system()
        src = self.fs.source_file
        with open(src + ".bad", "w") as fh:
            fh.write(f"{10 * BLOCK} {2 * BLOCK}\n")  # blocks 10 and 11 never read
        with open(src + ".flaky", "w") as fh:
            fh.write(f"{30 * BLOCK} {BLOCK}\n")  # block 30 reads on a retry
        self.plan = plan_mod.make_plan(self.fs, src, "/dev/fake-sdb1", None)
        self.assertTrue(self.plan["ok"], self.plan)
        self.blocks = self.plan["source"]["size"] // BLOCK
        os.environ["FAKE_DELAY"] = "0.002"
        os.environ.pop("FAKE_FAIL", None)

    def tearDown(self):
        os.environ.pop("FAKE_FAIL", None)
        super().tearDown()

    def job(self, plan=None, **opts):
        return ImageJob(self.fs, plan or self.plan, {"retries": 1, **opts}, cells=self.blocks)

    def assertImageMatches(self, bad):
        with open(self.fs.source_file, "rb") as a, open(self.plan["image"], "rb") as b:
            for n in range(self.blocks):
                a.seek(n * BLOCK)
                b.seek(n * BLOCK)
                want, got = a.read(BLOCK), b.read(BLOCK)
                if n in bad:
                    self.assertNotEqual(got, want, f"block {n} is damaged and must not appear copied")
                else:
                    self.assertEqual(got, want, f"block {n} differs")

    def test_full_run_with_retry(self):
        job = self.job()
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done", job.snapshot())
        snap = job.snapshot()
        self.assertFalse(snap["complete"])  # two blocks really are unreadable
        self.assertEqual(snap["skipped"], 2 * BLOCK)
        self.assertEqual(snap["percent"], round(100 * (1 - 2 / self.blocks), 1))
        self.assertEqual(snap["cells"][10:12], ["skipped", "skipped"])
        self.assertEqual(snap["cells"][30], "done")  # recovered by the retry pass
        self.assertEqual([p["status"] for p in snap["phases"]], ["done", "done", "done", "done"])
        self.assertImageMatches({10, 11})
        self.assertEqual(os.path.getsize(self.plan["image"]), self.plan["source"]["size"])
        with open(self.plan["jobfile"]) as fh:
            self.assertEqual(json.load(fh)["state"], "done")

    def test_quick_copy_only(self):
        job = self.job(retries=0)
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        snap = job.snapshot()
        self.assertEqual(snap["skipped"], 3 * BLOCK)  # the flaky block is not retried
        self.assertEqual(snap["phases"][2]["status"], "skipped")

    def test_a_clean_drive_completes(self):
        os.remove(self.fs.source_file + ".bad")
        os.remove(self.fs.source_file + ".flaky")
        job = self.job()
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        snap = job.snapshot()
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["percent"], 100.0)
        self.assertImageMatches(set())

    def test_source_is_locked_and_only_the_source(self):
        job = self.job()
        job.start()
        wait_for(job, self.TERMINAL)
        self.assertIn(("setro", self.fs.source_file), self.fs.calls)
        self.assertFalse([c for c in self.fs.calls if "fake-sdb" in str(c)], "nothing may be done to the healthy drive")

    def test_mounted_source_is_released_before_copying(self):
        def tweak(devs):
            devs[0]["children"] = [{"name": "sda1", "path": "/dev/fake-sda1", "type": "part", "size": 5000000,
                                    "fstype": "ntfs", "label": "Old", "mountpoints": ["/mnt/old"]}]
        self.fs.tweak = tweak
        plan = plan_mod.make_plan(self.fs, self.fs.source_file, "/dev/fake-sdb1", None)
        job = self.job(plan=plan)
        job.start()
        wait_for(job, self.TERMINAL)
        self.assertIn(("umount", "/mnt/old"), self.fs.calls)

    def test_pause_then_resume(self):
        os.environ["FAKE_DELAY"] = "0.05"
        job = self.job()
        job.start()
        end = time.time() + 20
        while job.snapshot()["percent"] < 10 and time.time() < end:
            time.sleep(0.05)
        job.pause()
        self.assertEqual(wait_for(job, self.TERMINAL), "paused")
        partial = job.snapshot()
        self.assertTrue(0 < partial["percent"] < 90, partial["percent"])
        os.environ["FAKE_DELAY"] = "0.002"
        job.resume()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        self.assertGreater(job.snapshot()["finished"], partial["finished"])
        self.assertImageMatches({10, 11})

    def test_a_new_session_continues_an_earlier_copy(self):
        os.environ["FAKE_DELAY"] = "0.05"
        first = self.job()
        first.start()
        end = time.time() + 20
        while first.snapshot()["percent"] < 10 and time.time() < end:
            time.sleep(0.05)
        first.stop()
        self.assertEqual(wait_for(first, self.TERMINAL), "stopped")
        os.environ["FAKE_DELAY"] = "0.002"
        again = plan_mod.make_plan(self.fs, self.fs.source_file, "/dev/fake-sdb1", None)
        self.assertTrue(again["ok"] and again["resume"], again)
        job = self.job(plan=again)
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        self.assertImageMatches({10, 11})

    def test_failure_is_reported_and_can_be_retried(self):
        os.environ["FAKE_FAIL"] = "1"
        job = self.job()
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "error")
        self.assertIn("stopped unexpectedly", job.snapshot()["message"])
        os.environ.pop("FAKE_FAIL")
        job.resume()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")

    def test_features_are_read_from_ddrescue_itself(self):
        ddrescue._FEATURES.clear()
        self.assertEqual(ddrescue.detect_features(self.fs, SHIM),
                         {"no_trim": True, "min_read_rate": True, "reopen": True, "retrim": True})
        ddrescue._FEATURES.clear()
        self.assertEqual(ddrescue.detect_features(self.fs, "/nonexistent/ddrescue"),
                         {"no_trim": False, "min_read_rate": False, "reopen": False, "retrim": False})
        ddrescue._FEATURES.clear()

    def test_the_job_uses_the_drives_sector_size_and_no_trim(self):
        job = self.job()
        job.plan["sector"] = 4096
        job.features = {"no_trim": True}
        cmd = job._command("quick")
        self.assertEqual(cmd[cmd.index("-b") + 1], "4096")
        self.assertIn("--no-trim", cmd)

    def test_the_backwards_pass_runs_and_is_shown_when_asked_for(self):
        job = self.job(thorough=True)
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        snap = job.snapshot()
        self.assertEqual([p["key"] for p in snap["phases"]], ["meta", "quick", "retry", "reverse", "final"])
        self.assertTrue(all(p["status"] == "done" for p in snap["phases"]))
        plain = self.job()
        self.assertNotIn("reverse", [p["key"] for p in plain.snapshot()["phases"]])

    # ---- extra care
    def test_health_advice_is_plain_and_matches_the_verdict(self):
        h = drives.check_health(self.fs, self.fs.source_file)
        self.assertEqual(h["status"], "failing")
        self.assertIn("failing", h["advice"])
        self.assertIn("can no longer read", h["advice"])
        self.assertIn("didn't report", drives.check_health(self.fs, "/dev/nothing")["advice"])

    def test_the_time_left_becomes_a_range_when_the_speed_is_all_over_the_place(self):
        job = self.job()
        job.size = 10 ** 13
        job.state = "running"
        job._t0 = job.clock()
        for i in range(30):
            job._live["rate"] = 100e6 if i % 2 else 5e6
            snap = job.snapshot()
        self.assertIn(" to ", snap["eta_text"])
        job2 = self.job()
        job2.size = 10 ** 13
        job2.state = "running"
        job2._t0 = job2.clock()
        for _ in range(30):
            job2._live["rate"] = 50e6
            snap2 = job2.snapshot()
        self.assertNotIn(" to ", snap2["eta_text"])

    def test_the_log_confirms_the_failing_drive_was_locked_and_sleep_is_blocked(self):
        lines = []
        job = ImageJob(self.fs, self.plan, {"retries": 0}, cells=self.blocks, log=lines.append)
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        self.assertIn("locked as read-only", "\n".join(lines))
        self.assertIn(("inhibit",), self.fs.calls)
        self.assertIn(("uninhibit",), self.fs.calls)

    def test_battery_use_is_reported(self):
        job = self.job()
        self.fs.power = {"on_battery": True, "percent": 41}
        self.assertEqual(job.snapshot()["power"], {"on_battery": True, "percent": 41})

    def test_a_drive_that_comes_back_under_a_new_name_is_found_again(self):
        job = self.job()
        job.start()
        wait_for(job, self.TERMINAL)
        job.state = "paused"
        real = self.fs.lsblk

        def renamed():
            devs = real()
            devs[0] = {**devs[0], "path": "/dev/fake-sdz", "name": "sdz"}
            return devs
        self.fs.lsblk = renamed
        job.resume()
        self.assertEqual(job.plan["source"]["path"], "/dev/fake-sdz")
        self.assertIn(("setro", "/dev/fake-sdz"), self.fs.calls)  # the new name is locked read-only too
        job.state = "done"

    # ---- safety while copying
    def test_resume_refuses_a_different_drive(self):
        os.environ["FAKE_DELAY"] = "0.05"
        job = self.job()
        job.start()
        job.pause()
        self.assertEqual(wait_for(job, self.TERMINAL), "paused")
        real = self.fs.lsblk

        def swapped():
            devs = real()
            devs[0] = {**devs[0], "serial": "SOMEONEELSE"}
            return devs
        self.fs.lsblk = swapped
        with self.assertRaises(JobError) as cm:
            job.resume()
        self.assertIn("not the same one", str(cm.exception))
        self.assertEqual(job.state, "paused")
        self.fs.lsblk = real
        os.environ["FAKE_DELAY"] = "0.002"
        job.resume()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")

    def test_resume_refuses_a_drive_that_is_gone(self):
        job = self.job()
        job.start()
        wait_for(job, self.TERMINAL)
        job.state = "paused"
        real = self.fs.lsblk
        self.fs.lsblk = lambda: [d for d in real() if d["path"] != self.fs.source_file]
        with self.assertRaises(JobError) as cm:
            job.resume()
        self.assertIn("isn't connected", str(cm.exception))

    def test_progress_for_a_different_sized_drive_is_not_reused(self):
        job = self.job()
        job.start()
        wait_for(job, self.TERMINAL)
        with open(self.plan["mapfile"], "a") as fh:
            fh.write("0x%08X  0x00010000  +\n" % (self.plan["source"]["size"] + 0x10000))  # runs past the end
        again = plan_mod.make_plan(self.fs, self.fs.source_file, "/dev/fake-sdb1", None)
        self.assertFalse(again["ok"])
        self.assertIn("different size", " ".join(again["errors"]))

    def test_a_stalled_drive_pauses_the_copy_by_itself(self):
        os.environ["FAKE_DELAY"] = "5"
        job = self.job()
        job.stall_limit, job.watch_tick = 0.6, 0.1
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL, timeout=15), "paused")
        snap = job.snapshot()
        self.assertEqual(snap["pause_reason"], "stall")
        self.assertIn("stopped responding", snap["message"])
        os.environ["FAKE_DELAY"] = "0.002"
        job.resume()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        self.assertEqual(job.snapshot()["pause_reason"], "")

    def test_a_hot_drive_pauses_the_copy_by_itself(self):
        os.environ["FAKE_DELAY"] = "0.05"
        self.fs.smart[self.fs.source_file]["temperature"]["current"] = 68
        job = self.job()
        job.heat_interval, job.watch_tick = 0.3, 0.1
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL, timeout=20), "paused")
        snap = job.snapshot()
        self.assertEqual(snap["pause_reason"], "heat")
        self.assertIn("68", snap["message"])

    def test_the_key_areas_are_copied_first_and_the_log_records_it(self):
        lines = []
        job = ImageJob(self.fs, self.plan, {"retries": 0}, cells=self.blocks, log=lines.append)
        self.assertEqual(job._phase_keys(), ["meta", "quick"])
        areas = job._meta_areas()
        size = self.plan["source"]["size"]
        self.assertEqual(areas[0][0], 0)
        self.assertEqual(areas[-1][0] + areas[-1][1], size)  # the end of the drive (backup partition table)
        job.start()
        self.assertEqual(wait_for(job, self.TERMINAL), "done")
        text = "\n".join(lines)
        self.assertIn("Key areas first", text)
        self.assertIn("Copy started", text)
        self.assertIn("Now done", text)

    def test_ntfs_index_and_partition_starts_are_key_areas(self):
        src = self.fs.source_file
        sector = 512
        with open(src, "r+b") as fh:
            boot = bytearray(512)
            boot[3:11] = b"NTFS    "
            boot[11:13] = (512).to_bytes(2, "little")
            boot[13] = 8
            boot[48:56] = (40).to_bytes(8, "little")  # the file index starts at cluster 40
            fh.seek(2048 * sector)
            fh.write(boot)
        from test_clone import build_gpt, DATA
        total = os.path.getsize(src) // sector
        build_gpt(src, total, [(DATA, 2048, total - 2048 - 1, "Data")])
        job = self.job()
        areas = job._meta_areas()
        starts = [a[0] for a in areas]
        self.assertTrue(any(a <= 2048 * sector < a + n for a, n in areas))          # the partition's own start
        self.assertTrue(any(a <= 2048 * sector + 40 * 8 * 512 < a + n for a, n in areas))  # the file index
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(areas[i][0] + areas[i][1] <= areas[i + 1][0] for i in range(len(areas) - 1)))

    def test_slow_areas_are_skipped_early_and_a_backwards_pass_is_optional(self):
        feats = {"no_trim": True, "min_read_rate": True, "reopen": True, "retrim": True}
        quick = ddrescue.build_command("d", "s", "i", "m", "quick", {"style": "fast", **feats})
        self.assertIn("--min-read-rate=65536", quick)
        self.assertIn("--reopen-on-error", quick)
        rev = ddrescue.build_command("d", "s", "i", "m", "reverse", {"style": "fast", **feats})
        self.assertIn("-R", rev)
        self.assertIn("--retrim", rev)
        self.assertNotIn("--min-read-rate=65536", rev)
        back = ddrescue.build_command("d", "s", "i", "m", "reverse", {"style": "fast", "direction": "backward"})
        self.assertNotIn("-R", back)  # the second look goes the other way round
        plain = ddrescue.build_command("d", "s", "i", "m", "quick", {"style": "fast"})
        self.assertFalse([a for a in plain if "min-read" in a or "reopen" in a])  # off unless supported
        job = self.job()
        self.assertEqual(job._phase_keys(), ["meta", "quick", "retry"])
        job.opts["thorough"] = True
        self.assertEqual(job._phase_keys(), ["meta", "quick", "retry", "reverse"])

    def test_a_drive_that_disappears_stops_the_copy_with_a_clear_message(self):
        os.environ["FAKE_DELAY"] = "0.05"
        job = self.job()
        job.start()
        end = time.time() + 20
        while job.snapshot()["percent"] < 3 and time.time() < end:
            time.sleep(0.05)
        os.remove(self.fs.source_file)  # unplugged
        self.assertEqual(wait_for(job, self.TERMINAL, timeout=30), "error")
        self.assertIn("disappeared", job.snapshot()["message"])

    def test_missing_ddrescue_is_explained(self):
        class NoTool(Tweaked):
            def which(self, name):
                return None
        s = NoTool()
        self.systems.append(s)
        job = ImageJob(s, self.plan, {})
        with self.assertRaises(JobError):
            job.start()

    def test_stuck_unmount_stops_before_any_copying(self):
        def tweak(devs):
            devs[0]["children"] = [{"name": "sda1", "path": "/dev/fake-sda1", "type": "part", "size": 5000000,
                                    "fstype": "ntfs", "label": "Old", "mountpoints": ["/mnt/old"]}]

        class Busy(Tweaked):
            def umount(self, mp):
                return False, "target is busy"
        s = Busy(tweak=tweak)
        plan_mod.MOUNT_ROOT = os.path.join(s.root, "mnt")
        self.systems.append(s)
        plan = plan_mod.make_plan(s, s.source_file, "/dev/fake-sdb1", None)
        with self.assertRaisesRegex(JobError, "busy"):
            ImageJob(s, plan, {}).start()
        self.assertFalse(os.path.exists(plan["image"]))


if __name__ == "__main__":
    unittest.main()
