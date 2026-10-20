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
