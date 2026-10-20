"""Run the real screens against a pretend machine (no real drives touched).

    python3 tests/demo.py [--port 8765] [--delay 0.05]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from drive_rescue import plan as plan_mod, recover, server  # noqa: E402
from drive_rescue.system import System  # noqa: E402
from test_engine import Tweaked  # noqa: E402
from test_clone import build_gpt, ESP, DATA, RECOVERY  # noqa: E402


class Demo(Tweaked):
    """Pretend drives, but the copy is opened with a real read-only loop mount."""

    def lsblk(self):
        devs = super().lsblk()
        for name, path, model, serial, size in (("sde", self.small, "Samsung 870 EVO 500GB", "S5Y1", 64 * 1024**2),
                                                 ("sdf", self.big, "Crucial MX500 1TB", "CT99", 192 * 1024**2)):
            devs.append({"name": name, "path": path, "type": "disk", "size": size, "model": model, "serial": serial,
                         "tran": "sata", "rota": False, "rm": False, "children": ([
                             {"name": name + "1", "path": path + "1", "type": "part", "size": 8 * 1024**2, "fstype": "vfat", "label": "EFI", "mountpoints": []},
                             {"name": name + "2", "path": path + "2", "type": "part", "size": 50 * 1024**2, "fstype": "ext4", "label": "Windows", "mountpoints": []},
                             {"name": name + "3", "path": path + "3", "type": "part", "size": 4 * 1024**2, "fstype": "ntfs", "label": "Recovery", "mountpoints": []},
                         ] if name == "sde" else [])})
        return devs

    def smartctl_json(self, path):
        if path in (self.small, self.big):
            return {"smart_status": {"passed": True}, "temperature": {"current": 33}}
        return super().smartctl_json(path)

    def mount(self, device, mountpoint, fstype=None, read_only=False):
        if device.startswith("/dev/loop"):
            return System.mount(self, device, mountpoint, fstype, read_only)
        return super().mount(device, mountpoint, fstype, read_only)

    def umount(self, mountpoint):
        if mountpoint.startswith(plan_mod.MOUNT_ROOT) and "image-" in mountpoint:
            return System.umount(self, mountpoint)
        return super().umount(mountpoint)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--delay", default="0.03")
    args = ap.parse_args()
    os.environ["FAKE_DELAY"] = args.delay
    s = Demo()
    plan_mod.MOUNT_ROOT = recover.MOUNT_ROOT = os.path.join(s.root, "mnt")
    tree = os.path.join(s.root, "tree")
    for d in ("Documents", "Photos/Holiday"):
        os.makedirs(os.path.join(tree, d))
    open(os.path.join(tree, "Documents", "Tax return.pdf"), "wb").write(os.urandom(250_000))
    open(os.path.join(tree, "Documents", "Notes.txt"), "w").write("hello\n")
    open(os.path.join(tree, "Photos", "Holiday", "beach.jpg"), "wb").write(os.urandom(900_000))
    subprocess.run(["truncate", "-s", "24M", s.source_file], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-L", "Family PC", "-d", tree, s.source_file], check=True)
    open(s.source_file + ".bad", "w").write("23068672 65536\n")  # a pretend damaged patch
    s.small, s.big = os.path.join(s.root, "small.disk"), os.path.join(s.root, "big.disk")
    subprocess.run(["truncate", "-s", "64M", s.small], check=True)
    subprocess.run(["truncate", "-s", "192M", s.big], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-L", "Windows", "-d", tree, "-E", "offset=%d" % (18432 * 512), s.small, "51200k"], check=True)
    build_gpt(s.small, 64 * 1024**2 // 512, [(ESP, 2048, 18431, "EFI"), (DATA, 18432, 120831, "Windows"), (RECOVERY, 120832, 129023, "Recovery")])
    app = server.App(s)
    httpd, url = server.serve(app, port=args.port)
    print("Demo running at", url, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        shutil.rmtree(s.root, ignore_errors=True)


if __name__ == "__main__":
    main()
