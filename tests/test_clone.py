"""Tests for clone mode: partition resizing on real disk-image files, the safety gate, the job."""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from drive_rescue import clone, partitions, plan as plan_mod  # noqa: E402
from drive_rescue.system import System  # noqa: E402
from fake import SHIM, FakeSystem  # noqa: E402

S = 512
MIB = 1024 * 1024
ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
RECOVERY = partitions.GPT_RECOVERY


def have_loop():
    if os.geteuid() != 0 or not shutil.which("mkfs.ext4") or not shutil.which("resize2fs"):
        return False
    tmp = tempfile.mkdtemp()
    try:
        img = os.path.join(tmp, "t")
        subprocess.run(["truncate", "-s", "4M", img], check=True)
        loop, _ = System().attach_region(img, 0, 4 * MIB)
        if loop:
            System().detach_image(loop)
        return bool(loop)
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_gpt(path, total, parts):
    """An independent GPT writer for the tests (does not share code with drive_rescue)."""
    entries = bytearray(128 * 128)
    for i, (ptype, start, end, name) in enumerate(parts):
        e = bytearray(128)
        e[:16] = uuid.UUID(ptype).bytes_le
        e[16:32] = uuid.uuid4().bytes_le
        struct.pack_into("<QQQ", e, 32, start, end, 0)
        e[56:56 + 2 * len(name)] = name.encode("utf-16le")
        entries[i * 128:(i + 1) * 128] = e
    ecrc = zlib.crc32(bytes(entries))
    guid = uuid.uuid4().bytes_le

    def header(my, alt, elba):
        h = bytearray(92)
        h[:8] = b"EFI PART"
        struct.pack_into("<IIIIQQQQ", h, 8, 0x10000, 92, 0, 0, my, alt, 34, total - 34)
        h[56:72] = guid
        struct.pack_into("<QIII", h, 72, elba, 128, 128, ecrc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)))
        return bytes(h).ljust(S, b"\0")

    mbr = bytearray(S)
    mbr[446 + 4] = 0xEE
    struct.pack_into("<II", mbr, 446 + 8, 1, min(total - 1, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xAA"
    with open(path, "r+b") as fh:
        fh.write(bytes(mbr))
        fh.seek(S); fh.write(header(1, total - 1, 2))
        fh.seek(2 * S); fh.write(entries)
        fh.seek((total - 33) * S); fh.write(entries)
        fh.seek((total - 1) * S); fh.write(header(total - 1, 1, total - 33))


def check_gpt(case, path, total, expect_last_usable=None):
    """Verify both GPT copies the way firmware would."""
    with open(path, "rb") as fh:
        for lba, my, alt in ((1, 1, total - 1), (total - 1, total - 1, 1)):
            fh.seek(lba * S)
            h = bytearray(fh.read(92))
            case.assertEqual(bytes(h[:8]), b"EFI PART")
            crc = struct.unpack_from("<I", h, 16)[0]
            h[16:20] = b"\0\0\0\0"
            case.assertEqual(zlib.crc32(bytes(h)), crc, "header checksum")
            my_, alt_, first, last = struct.unpack_from("<QQQQ", h, 24)
            case.assertEqual((my_, alt_), (my, alt))
            case.assertEqual(last, total - 34)
            elba, num, esz, ecrc = struct.unpack_from("<QIII", h, 72)
            fh.seek(elba * S)
            case.assertEqual(zlib.crc32(fh.read(num * esz)), ecrc, "entries checksum")
        fh.seek(0)
        mbr = fh.read(S)
        case.assertEqual(struct.unpack_from("<I", mbr, 446 + 12)[0], min(total - 1, 0xFFFFFFFF))


@unittest.skipUnless(have_loop(), "needs root, ext4 tools and loop devices")
class CloneCommandSectorTests(unittest.TestCase):
    def test_4k_drives_are_told_their_sector_size(self):
        cmd = clone.build_clone_command("ddr", "/dev/a", "/dev/b", "m", "quick", {}, sector=4096)
        self.assertEqual(cmd, ["ddr", "-f", "-d", "-b", "4096", "-n", "/dev/a", "/dev/b", "m"])
        self.assertNotIn("-b", clone.build_clone_command("ddr", "/dev/a", "/dev/b", "m", "quick", {}))


class ResizeTests(unittest.TestCase):
    SRC_MB, DST_MB = 64, 192

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clone-test-")
        self.sysm = System()
        self.src = os.path.join(self.tmp, "src.disk")
        subprocess.run(["truncate", "-s", f"{self.SRC_MB}M", self.src], check=True)
        total = self.SRC_MB * MIB // S
        tree = os.path.join(self.tmp, "tree")
        os.makedirs(tree)
        with open(os.path.join(tree, "keep.txt"), "w") as fh:
            fh.write("precious\n")
        self.main_start, self.rec_start = 18432, 120832
        main_sectors = self.rec_start - self.main_start
        subprocess.run(["mkfs.ext4", "-q", "-F", "-L", "Windows", "-d", tree, "-E", f"offset={self.main_start * S}",
                        self.src, f"{main_sectors * S // 1024}k"], check=True)
        self.rec_data = os.urandom(8192 * S)
        with open(self.src, "r+b") as fh:
            fh.seek(self.rec_start * S)
            fh.write(self.rec_data)
        build_gpt(self.src, total, [(ESP, 2048, 18431, "EFI"), (DATA, self.main_start, self.rec_start - 1, "Windows"),
                                    (RECOVERY, self.rec_start, self.rec_start + 8191, "Recovery")])
        self.dst = os.path.join(self.tmp, "dst.disk")
        shutil.copy(self.src, self.dst)  # what a clone leaves behind
        subprocess.run(["truncate", "-s", f"{self.DST_MB}M", self.dst], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_whole_bigger_drive_becomes_usable(self):
        before = partitions.read_layout(self.dst)
        self.assertEqual(before["hdr"]["alt"], self.SRC_MB * MIB // S - 1)  # the stale picture of the small drive
        res = clone.finish_layout(self.sysm, self.dst)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["extended"])
        self.assertTrue(res["fs_ok"], res)
        total = self.DST_MB * MIB // S
        check_gpt(self, self.dst, total)
        now = partitions.read_layout(self.dst)
        main = next(p for p in now["parts"] if p["name"] == "Windows")
        rec = next(p for p in now["parts"] if p["name"] == "Recovery")
        self.assertEqual(rec["end"] - rec["start"] + 1, 8192)
        self.assertEqual(main["end"] + 1, rec["start"])       # the main partition runs right up to recovery
        self.assertLessEqual(partitions.usable_last(now, total) - rec["end"], 2048)  # recovery is at the very end
        self.assertGreater(main["end"] - main["start"], 3 * (self.rec_start - self.main_start))
        with open(self.dst, "rb") as fh:                      # the moved partition arrived intact
            fh.seek(rec["start"] * S)
            self.assertEqual(fh.read(8192 * S), self.rec_data)
        # the filesystem really fills the partition and is clean
        loop, _ = self.sysm.attach_region(self.dst, main["start"] * S, (main["end"] - main["start"] + 1) * S)
        try:
            rc, out, err = self.sysm.run(["e2fsck", "-fn", loop], timeout=120)
            self.assertEqual(rc, 0, out + err)
            rc, out, _ = self.sysm.run(["dumpe2fs", "-h", loop], timeout=30)
            blocks = int(next(l.split(":")[1] for l in out.splitlines() if l.startswith("Block count")))
            bsize = int(next(l.split(":")[1] for l in out.splitlines() if l.startswith("Block size")))
            self.assertGreater(blocks * bsize, 0.97 * (main["end"] - main["start"] + 1) * S)
            mp = os.path.join(self.tmp, "mnt")
            ok, msg = self.sysm.mount(loop, mp, "ext4", read_only=True)
            self.assertTrue(ok, msg)
            try:
                self.assertEqual(open(os.path.join(mp, "keep.txt")).read(), "precious\n")
            finally:
                self.sysm.umount(mp)
        finally:
            self.sysm.detach_image(loop)

    def test_same_size_drive_changes_nothing(self):
        same = os.path.join(self.tmp, "same.disk")
        shutil.copy(self.src, same)
        before = open(same, "rb").read()
        res = clone.finish_layout(self.sysm, same)
        self.assertFalse(res["extended"])
        self.assertEqual(open(same, "rb").read(), before)

    def test_last_partition_grows_when_there_is_no_recovery(self):
        build_gpt(self.dst, self.SRC_MB * MIB // S, [(ESP, 2048, 18431, "EFI"), (DATA, self.main_start, self.rec_start - 1, "Windows")])
        res = clone.finish_layout(self.sysm, self.dst)
        self.assertTrue(res["ok"] and res["extended"] and res["fs_ok"], res)
        now = partitions.read_layout(self.dst)
        self.assertEqual(now["parts"][-1]["end"], self.DST_MB * MIB // S - 34)
        check_gpt(self, self.dst, self.DST_MB * MIB // S)

    def test_mbr_partition_grows(self):
        disk = os.path.join(self.tmp, "mbr.disk")
        subprocess.run(["truncate", "-s", "32M", disk], check=True)
        subprocess.run(["mkfs.ext4", "-q", "-F", "-E", "offset=1048576", disk, "20480k"], check=True)
        mbr = bytearray(S)
        mbr[446 + 4] = 0x83
        struct.pack_into("<II", mbr, 446 + 8, 2048, 40960)
        mbr[510:512] = b"\x55\xAA"
        with open(disk, "r+b") as fh:
            fh.write(mbr)
        subprocess.run(["truncate", "-s", "96M", disk], check=True)
        res = clone.finish_layout(self.sysm, disk)
        self.assertTrue(res["ok"] and res["extended"] and res["fs_ok"], res)
        now = partitions.read_layout(disk)
        self.assertEqual(now["kind"], "mbr")
        self.assertEqual(now["parts"][0]["end"], 96 * MIB // S - 1)

    def test_filesystem_it_cannot_grow_is_left_alone(self):
        layout = partitions.read_layout(self.dst)
        slots = {p["slot"]: "exfat" for p in layout["parts"]}
        plan = partitions.plan_growth(layout, self.DST_MB * MIB // S, slots)
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["extends"])
        self.assertIn("can't be extended", plan["reason"])

    def test_damaged_or_missing_tables_are_not_touched(self):
        junk = os.path.join(self.tmp, "junk.disk")
        with open(junk, "wb") as fh:
            fh.write(os.urandom(2 * MIB))
        before = open(junk, "rb").read()
        res = clone.finish_layout(self.sysm, junk)
        self.assertFalse(res["extended"])
        self.assertEqual(open(junk, "rb").read(), before)

    def test_overlapping_move_is_refused(self):
        with self.assertRaises(partitions.TableError):
            partitions.move_data(self.dst, S, 100, 150, 100)


class Pair(FakeSystem):
    """Two file-backed drives: a healthy 'small' source and a bigger target."""

    def __init__(self, dest_factor=3, **kw):
        super().__init__(**kw)
        os.chmod(SHIM, 0o755)
        self.dest_file = os.path.join(self.root, "target.disk")
        with open(self.dest_file, "wb") as fh:
            fh.truncate(os.path.getsize(self.source_file) * dest_factor)
        self.smart[self.source_file] = {"smart_status": {"passed": True}, "temperature": {"current": 30},
                                       "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 0}}]}}
        self.sector = 512
        self.tweak = None

    def sector_size(self, path):
        return self.sector if path == self.dest_file else 512

    def lsblk(self):
        devs = super().lsblk()
        src = devs[0]
        src["model"], src["serial"] = "Small SSD", "SRC0001"
        src["children"] = [{"name": "sda1", "path": self.source_file + "1", "type": "part", "size": 1,
                            "fstype": "ntfs", "label": "C", "mountpoints": []}]
        devs.insert(1, {"name": "sdd", "path": self.dest_file, "type": "disk", "size": os.path.getsize(self.dest_file),
                        "model": "Big SSD", "serial": "DST0002", "tran": "sata", "rota": False, "rm": False,
                        "children": [{"name": "sdd1", "path": self.dest_file + "1", "type": "part", "size": 5000,
                                      "fstype": "ntfs", "label": "Old stuff", "mountpoints": []}]})
        if self.tweak:
            self.tweak(devs)
        return devs


class GateTests(unittest.TestCase):
    def setUp(self):
        self._work = clone.WORK_DIR
        self.sysm = Pair()
        clone.WORK_DIR = os.path.join(self.sysm.root, "work")

    def tearDown(self):
        clone.WORK_DIR = self._work
        self.sysm.cleanup()

    def plan(self, src=None, dst=None):
        return clone.make_clone_plan(self.sysm, src or self.sysm.source_file, dst or self.sysm.dest_file)

    def test_healthy_pair_is_allowed_and_dest_contents_are_listed(self):
        p = self.plan()
        self.assertTrue(p["ok"], p["errors"])
        self.assertEqual(p["health"]["status"], "healthy")
        self.assertEqual(p["dest_contents"][0]["name"], "Old stuff")

    def test_smaller_target_is_refused(self):
        p = self.plan(src=self.sysm.dest_file, dst=self.sysm.source_file)
        self.assertFalse(p["ok"])
        self.assertIn("smaller", p["errors"][0])

    def test_failing_source_is_refused_with_advice(self):
        self.sysm.smart[self.sysm.source_file] = {"smart_status": {"passed": False}}
        p = self.plan()
        self.assertFalse(p["ok"])
        self.assertIn("Rescue files", p["errors"][0])

    def test_worn_or_unknown_health_only_warns(self):
        self.sysm.smart[self.sysm.source_file] = {"smart_status": {"passed": True},
                                                  "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 9}}]}}
        p = self.plan()
        self.assertTrue(p["ok"])
        self.assertTrue(p["warnings"])
        del self.sysm.smart[self.sysm.source_file]
        self.assertTrue(self.plan()["ok"])

    def test_same_drive_boot_stick_and_missing_drives_are_refused(self):
        self.assertFalse(self.plan(dst=self.sysm.source_file)["ok"])
        self.assertFalse(self.plan(dst="/dev/fake-sdc")["ok"])
        self.assertFalse(self.plan(src="/dev/fake-sdc")["ok"])
        self.assertFalse(self.plan(dst="/dev/nope")["ok"])

    def test_different_sector_sizes_are_refused(self):
        self.sysm.sector = 4096
        p = self.plan()
        self.assertFalse(p["ok"])
        self.assertIn("sector sizes", p["errors"][0])

    def test_unreadable_source_is_refused(self):
        self.sysm.tweak = lambda devs: devs[0].update(path="/nonexistent/disk")
        p = self.plan(src="/nonexistent/disk")
        self.assertFalse(p["ok"])

    def test_equal_size_warns_there_is_no_gain(self):
        big = Pair(dest_factor=1)
        try:
            clone.WORK_DIR = os.path.join(big.root, "w")
            p = clone.make_clone_plan(big, big.source_file, big.dest_file)
            self.assertTrue(p["ok"])
            self.assertTrue(any("same size" in w for w in p["warnings"]))
        finally:
            big.cleanup()

    def test_clone_command_is_the_only_one_with_force(self):
        cmd = clone.build_clone_command("ddrescue", "/dev/a", "/dev/b", "m", "quick", {})
        self.assertIn("-f", cmd)
        from drive_rescue import ddrescue
        for phase in ("quick", "retry"):
            self.assertNotIn("-f", ddrescue.build_command("ddrescue", "/dev/a", "x.img", "m", phase, {}))


class JobTests(unittest.TestCase):
    def setUp(self):
        self._work = clone.WORK_DIR
        self.sysm = Pair(source_size=8 * MIB)
        clone.WORK_DIR = os.path.join(self.sysm.root, "work")

    def tearDown(self):
        clone.WORK_DIR = self._work
        self.sysm.cleanup()

    def run_job(self, plan):
        job = clone.CloneJob(self.sysm, plan, {})
        job.start()
        end = time.time() + 60
        while time.time() < end and job.state not in ("done", "error", "stopped"):
            time.sleep(0.1)
        return job

    def test_clone_copies_everything_onto_the_bigger_drive(self):
        plan = clone.make_clone_plan(self.sysm, self.sysm.source_file, self.sysm.dest_file)
        job = self.run_job(plan)
        self.assertEqual(job.state, "done", job.message)
        size = os.path.getsize(self.sysm.source_file)
        with open(self.sysm.source_file, "rb") as a, open(self.sysm.dest_file, "rb") as b:
            self.assertEqual(a.read(), b.read(size))
        self.assertEqual(os.path.getsize(self.sysm.dest_file), size * 3)  # the target keeps its full size
        snap = job.snapshot()
        self.assertEqual(snap["mode"], "clone")
        self.assertEqual([p["key"] for p in snap["phases"]], ["copy", "layout"])
        self.assertIn(("setro", self.sysm.source_file), self.sysm.calls)
        self.assertNotIn(("setro", self.sysm.dest_file), self.sysm.calls)

    def test_a_drive_with_unreadable_areas_is_not_resized(self):
        with open(self.sysm.source_file + ".bad", "w") as fh:
            fh.write("1048576 131072\n")
        plan = clone.make_clone_plan(self.sysm, self.sysm.source_file, self.sysm.dest_file)
        job = self.run_job(plan)
        snap = job.snapshot()
        self.assertEqual(job.state, "done")
        self.assertFalse(snap["result"]["ok"])
        self.assertIn("incomplete", snap["result"]["warnings"][0])

    def test_a_stale_mapfile_from_another_pair_is_never_reused(self):
        plan = clone.make_clone_plan(self.sysm, self.sysm.source_file, self.sysm.dest_file)
        os.makedirs(clone.WORK_DIR, exist_ok=True)
        with open(plan["mapfile"], "w") as fh:  # claims everything is already copied
            fh.write("# x\n0x00000000     ?               1\n#      pos        size  status\n"
                     f"0x00000000  0x{os.path.getsize(self.sysm.source_file):08X}  +\n")
        job = self.run_job(plan)
        with open(self.sysm.source_file, "rb") as a, open(self.sysm.dest_file, "rb") as b:
            self.assertEqual(a.read(), b.read(os.path.getsize(self.sysm.source_file)))


if __name__ == "__main__":
    unittest.main()
