import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from drive_rescue import recover  # noqa: E402
from drive_rescue.system import System  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def loop_mount_works():
    """Real loop mounting needs root and a kernel that allows it."""
    if os.geteuid() != 0 or not shutil.which("mkfs.ext4"):
        return False
    tmp = tempfile.mkdtemp()
    try:
        img = os.path.join(tmp, "t.img")
        subprocess.run(["truncate", "-s", "8M", img], check=True)
        subprocess.run(["mkfs.ext4", "-q", img], check=True)
        sysm = System()
        loop, _ = sysm.attach_image(img)
        if not loop:
            return False
        mp = os.path.join(tmp, "m")
        ok, _ = sysm.mount(loop, mp, "ext4", read_only=True)
        if ok:
            sysm.umount(mp)
        sysm.detach_image(loop)
        return ok
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(loop_mount_works(), "real loop mounts are not available here")
class RealImageTests(unittest.TestCase):
    """Uses a real ext4 image opened through a real loop device."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recover-test-")
        tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(tree, "Documents"))
        os.makedirs(os.path.join(tree, "Photos"))
        with open(os.path.join(tree, "Documents", "report.txt"), "w") as fh:
            fh.write("hello report\n")
        with open(os.path.join(tree, "Photos", "img1.jpg"), "wb") as fh:
            fh.write(os.urandom(300_000))
        os.symlink("/etc", os.path.join(tree, "escape"))
        self.image = os.path.join(self.tmp, "disk.img")
        subprocess.run(["truncate", "-s", "20M", self.image], check=True)
        subprocess.run(["mkfs.ext4", "-q", "-L", "Family", "-d", tree, self.image], check=True)
        self.tree = tree
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)
        self._old_root = recover.MOUNT_ROOT
        recover.MOUNT_ROOT = os.path.join(self.tmp, "mnt")
        self.rec = recover.Recovery(System(), self.image, self.out)

    def tearDown(self):
        self.rec.close()
        recover.MOUNT_ROOT = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_task(self):
        end = time.time() + 20
        while time.time() < end and self.rec.status()["state"] == "running":
            time.sleep(0.05)

    def test_open_browse_and_copy(self):
        info = self.rec.open()
        self.assertTrue(info["readable"], info)
        self.assertEqual(info["volumes"][0]["fstype"], "ext4")
        vol = info["volumes"][0]["id"]
        top = {e["name"]: e for e in self.rec.list_dir(vol)["entries"]}
        self.assertTrue(top["Documents"]["is_dir"])
        self.assertFalse(top["escape"]["is_dir"], "symlinks must never be followed")
        docs = self.rec.list_dir(vol, "Documents")["entries"]
        self.assertEqual([e["name"] for e in docs], ["report.txt"])

        self.rec.start_copy([{"volume": vol, "rel": "Photos"}, {"volume": vol, "rel": "Documents/report.txt"},
                             {"volume": vol, "rel": "escape"}])
        self.wait_task()
        st = self.rec.status()
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["copied"], 2)
        base = os.path.join(self.out, "recovered", "Family")
        with open(os.path.join(base, "Photos", "img1.jpg"), "rb") as a, \
                open(os.path.join(self.tree, "Photos", "img1.jpg"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(open(os.path.join(base, "Documents", "report.txt")).read(), "hello report\n")
        self.assertFalse(os.path.exists(os.path.join(base, "escape")))

    def test_path_traversal_is_refused(self):
        info = self.rec.open()
        vol = info["volumes"][0]["id"]
        for bad in ("../../etc", "escape/passwd", "Documents/../../x"):
            with self.assertRaises(recover.RecoverError):
                self.rec.list_dir(vol, bad)

    def test_the_image_is_opened_read_only(self):
        self.rec.open()
        vol = self.rec.volumes[0]
        with self.assertRaises(OSError):
            open(os.path.join(vol["mountpoint"], "new.txt"), "w")


class FakeToolTests(unittest.TestCase):
    def test_carve_plumbing(self):
        tmp = tempfile.mkdtemp()
        try:
            class S(System):
                def which(self, name):
                    return os.path.join(HERE, "fake_photorec.py") if name == "photorec" else None
            os.chmod(os.path.join(HERE, "fake_photorec.py"), 0o755)
            rec = recover.Recovery(S(), os.path.join(tmp, "x.img"), tmp)
            rec.start_carve()
            end = time.time() + 10
            while time.time() < end and rec.status()["state"] == "running":
                time.sleep(0.05)
            st = rec.status()
            self.assertEqual(st["state"], "done", st)
            self.assertEqual(st["copied"], 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_carve_without_the_tool_explains_itself(self):
        class S(System):
            def which(self, name):
                return None
        with self.assertRaises(recover.RecoverError):
            recover.Recovery(S(), "/x.img", "/tmp").start_carve()

    def test_safe_join(self):
        self.assertEqual(recover.safe_join("/tmp", "a/b"), os.path.realpath("/tmp/a/b"))
        with self.assertRaises(recover.RecoverError):
            recover.safe_join("/tmp", "../etc")


if __name__ == "__main__":
    unittest.main()


class DamageCheckTests(unittest.TestCase):
    """Unrecovered parts of the copy must show up as damaged files, never as silent blanks."""

    def test_the_table_turns_unrecovered_parts_into_errors(self):
        table, bad = recover.damage_table([(0, 1024, "+"), (1024, 512, "-"), (1536, 2560, "+")], 0, 4096, "/dev/x")
        self.assertEqual(table.split("\n")[:3], ["0 2 linear /dev/x 0", "2 1 error", "3 5 linear /dev/x 3"])
        self.assertEqual(bad, 512)

    def test_nothing_missing_means_nothing_to_hide(self):
        table, bad = recover.damage_table([(0, 4096, "+")], 0, 4096, "d")
        self.assertEqual((table.strip(), bad), ("0 8 linear d 0", 0))

    def test_good_areas_shrink_and_bad_areas_grow_to_whole_sectors(self):
        table, bad = recover.damage_table([(0, 1000, "+")], 0, 2048, "d")  # 1000 bytes is not a whole sector
        self.assertEqual(table.strip().split("\n"), ["0 1 linear d 0", "1 3 error"])
        self.assertEqual(bad, 1536)

    def test_a_partition_inside_the_image_is_measured_from_its_own_start(self):
        table, bad = recover.damage_table([(4096, 4096, "+")], 2048, 4096, "d")
        self.assertEqual(table.strip().split("\n"), ["0 4 error", "4 4 linear d 4"])
        self.assertEqual(bad, 2048)

    def test_untried_gaps_count_as_missing(self):
        _table, bad = recover.damage_table([(0, 1024, "+")], 0, 4096, "d")
        self.assertEqual(bad, 3072)

    def _fake(self, tmp, mapfile_text):
        calls = []

        class Sys(System):
            def attach_image(self, image):
                return "/dev/loop9", ""

            def detach_image(self, loop):
                calls.append(("detach", loop))

            def lsblk(self):
                return [{"path": "/dev/loop9", "name": "loop9", "children": [
                    {"path": "/dev/loop9p1", "name": "loop9p1", "type": "part", "fstype": "ext4",
                     "label": "Family", "size": 4096}]}]

            def partition_offset(self, path):
                return 0

            def mount(self, device, mountpoint, fstype=None, read_only=False):
                calls.append(("mount", device))
                os.makedirs(mountpoint, exist_ok=True)
                return True, ""

            def umount(self, mountpoint):
                return True, ""

            def dm_create(self, name, table):
                calls.append(("dm_create", name))
                return "/dev/mapper/" + name, ""

            def dm_remove(self, name):
                calls.append(("dm_remove", name))

        mf = os.path.join(tmp, "m.map")
        open(mf, "w").write(mapfile_text)
        return Sys(), mf, calls

    def test_a_copy_with_missing_parts_is_opened_through_the_error_view(self):
        tmp = tempfile.mkdtemp()
        old, recover.MOUNT_ROOT = recover.MOUNT_ROOT, os.path.join(tmp, "mnt")
        try:
            sysm, mf, calls = self._fake(tmp, "0x0 + 1\n0x0 1024 +\n1024 3072 -\n")
            rec = recover.Recovery(sysm, "x.img", tmp, mf)
            info = rec.open()
            self.assertTrue(info["readable"])
            self.assertFalse(info["unchecked"])
            self.assertIn(("mount", "/dev/mapper/dr-image-%d-0" % os.getpid()), calls)
            self.assertEqual(rec.volumes[0]["damaged_bytes"], 3072)
            rec.close()
            self.assertIn(("dm_remove", "dr-image-%d-0" % os.getpid()), calls)
        finally:
            recover.MOUNT_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_complete_copy_is_opened_plainly(self):
        tmp = tempfile.mkdtemp()
        old, recover.MOUNT_ROOT = recover.MOUNT_ROOT, os.path.join(tmp, "mnt")
        try:
            sysm, mf, calls = self._fake(tmp, "0x0 + 1\n0x0 4096 +\n")
            rec = recover.Recovery(sysm, "x.img", tmp, mf)
            rec.open()
            self.assertIn(("mount", "/dev/loop9p1"), calls)
            self.assertFalse([c for c in calls if c[0] == "dm_create"])
            self.assertEqual(rec.volumes[0]["damage_check"], True)
        finally:
            recover.MOUNT_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_when_the_check_cannot_be_set_up_the_person_is_told(self):
        tmp = tempfile.mkdtemp()
        old, recover.MOUNT_ROOT = recover.MOUNT_ROOT, os.path.join(tmp, "mnt")
        try:
            sysm, mf, calls = self._fake(tmp, "0x0 + 1\n0x0 1024 +\n1024 3072 -\n")
            sysm.dm_create = lambda name, table: (None, "no dmsetup")
            rec = recover.Recovery(sysm, "x.img", tmp, mf)
            self.assertTrue(rec.open()["unchecked"])
        finally:
            recover.MOUNT_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_file_with_unreadable_pieces_is_kept_but_flagged(self):
        import errno
        tmp = tempfile.mkdtemp()
        try:
            src, dst = os.path.join(tmp, "src.bin"), os.path.join(tmp, "out", "dst.bin")
            with open(src, "wb") as fh:
                fh.write(b"A" * (3 * 4096))
            rec = recover.Recovery(System(), "x.img", tmp)
            rec.task = {"kind": "copy", "state": "running", "copied": 0, "bytes": 0, "failed": [],
                        "current": "", "message": "", "damaged": []}
            real = os.pread

            def flaky(fd, n, off):
                if off <= 4096 < off + n:  # the second 4 KB piece sits in a damaged area
                    if n > 4096:
                        raise OSError(errno.EIO, "io")
                    if off == 4096:
                        raise OSError(errno.EIO, "io")
                return real(fd, n, off)
            os.pread = flaky
            try:
                rec._copy_one(src, dst)
            finally:
                os.pread = real
            data = open(dst, "rb").read()
            self.assertEqual(len(data), 3 * 4096)
            self.assertEqual(data[:4096], b"A" * 4096)
            self.assertEqual(data[4096:8192], b"\0" * 4096)
            self.assertEqual(data[8192:], b"A" * 4096)
            self.assertEqual(rec.task["damaged"][0]["bad_bytes"], 4096)
            self.assertEqual(rec.task["copied"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_file_that_cannot_be_read_at_all_is_not_kept(self):
        import errno
        tmp = tempfile.mkdtemp()
        try:
            src, dst = os.path.join(tmp, "src.bin"), os.path.join(tmp, "out", "dst.bin")
            open(src, "wb").write(b"A" * 8192)
            rec = recover.Recovery(System(), "x.img", tmp)
            rec.task = {"kind": "copy", "state": "running", "copied": 0, "bytes": 0, "failed": [],
                        "current": "", "message": "", "damaged": []}
            real = os.pread
            os.pread = lambda fd, n, off: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
            try:
                with self.assertRaises(OSError):
                    rec._copy_one(src, dst)
            finally:
                os.pread = real
            self.assertFalse(os.path.exists(dst))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
