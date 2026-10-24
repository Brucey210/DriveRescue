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


def photo_with_thumbnail(color):
    """A pretend camera photo: junk picture data, but a real small preview inside it."""
    import io
    import struct
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (160, 120), color).save(buf, "JPEG")
    thumb = buf.getvalue()
    tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 0) + struct.pack(">I", 14)
    tiff += (struct.pack(">H", 2) + struct.pack(">HHII", 0x0201, 4, 1, 14 + 2 + 24 + 4)
             + struct.pack(">HHII", 0x0202, 4, 1, len(thumb)) + struct.pack(">I", 0)) + thumb
    app1 = b"Exif\0\0" + tiff
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xdb\0\x04\0\0\xff\xda" + os.urandom(300_000)


def add_saved_copy(s):
    """A copy made on another day, sitting on the healthy drive, so "Browse a saved copy" has something to open."""
    tree = os.path.join(s.root, "oldtree")
    for d in ("Pictures/2019", "Work"):
        os.makedirs(os.path.join(tree, d))
    for i, color in enumerate(("#e8a63a", "#12968f", "#c0392b")):
        open(os.path.join(tree, "Pictures", "2019", f"IMG_{4200 + i}.jpg"), "wb").write(photo_with_thumbnail(color))
    open(os.path.join(tree, "Work", "Budget 2019.xlsx"), "wb").write(os.urandom(90_000))
    open(os.path.join(tree, "Work", "Letter.txt"), "w").write("Dear Sam,\n")
    folder = os.path.join(s.dest_mount, "OldLaptop")
    os.makedirs(folder)
    img = os.path.join(folder, "Toshiba-MQ01-1a2b.img")
    subprocess.run(["truncate", "-s", "24M", img], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-L", "Old laptop", "-d", tree, img], check=True)
    size = 24 * 1024 * 1024
    open(img[:-4] + ".map", "w").write(f"0x0 + 1\n0x0 {size} +\n")
    open(img[:-4] + ".job.json", "w").write('{"source": {"model": "Toshiba MQ01 500GB", "serial": "1A2B", "size": %d}}' % size)


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
    add_saved_copy(s)
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
