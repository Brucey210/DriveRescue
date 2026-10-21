"""A fake machine for tests: regular files stand in for drives. No real disk is touched."""
import os
import shutil
import tempfile

from drive_rescue.system import System

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(HERE, "fake_ddrescue.py")


class FakeSystem(System):
    def __init__(self, root=None, source_size=8 * 1024 * 1024, dest_fs="ntfs", boot_label="RESCUE1100"):
        self.root = root or tempfile.mkdtemp(prefix="fake-rescue-")
        self.source_file = os.path.join(self.root, "failing.disk")
        with open(self.source_file, "wb") as fh:
            block = bytes(range(256)) * 256  # 64 KiB of recognisable, non-zero data
            for i in range(source_size // len(block)):
                fh.write(bytes([(b + i) % 251 + 1 for b in block[:16]]) + block[16:])
        self.dest_mount = os.path.join(self.root, "healthy-mount")
        os.makedirs(self.dest_mount, exist_ok=True)
        self.dest_fs = dest_fs
        self.boot_label = boot_label
        self.calls = []
        self.smart = {self.source_file: {
            "smart_status": {"passed": False},
            "ata_smart_attributes": {"table": [
                {"id": 5, "raw": {"value": 1204}}, {"id": 197, "raw": {"value": 96}}]},
            "temperature": {"current": 41}}}

    def lsblk(self):
        size = os.path.getsize(self.source_file)
        return [
            {"name": "sda", "path": self.source_file, "type": "disk", "size": size, "model": "Fake Failing HDD",
             "serial": "WX1234ABCD", "tran": "sata", "rota": True, "rm": False, "mountpoints": [None], "children": []},
            {"name": "sdb", "path": "/dev/fake-sdb", "type": "disk", "size": 4_000_000_000_000, "model": "Fake Healthy HDD",
             "serial": "ZZ9999EFGH", "tran": "usb", "rota": True, "rm": False, "children": [
                 {"name": "sdb1", "path": "/dev/fake-sdb1", "type": "part", "size": 3_999_000_000_000,
                  "fstype": self.dest_fs, "label": "Backup", "mountpoints": [self.dest_mount]}]},
            {"name": "sdc", "path": "/dev/fake-sdc", "type": "disk", "size": 32_000_000_000, "model": "Rescue USB",
             "tran": "usb", "rota": False, "rm": True, "children": [
                 {"name": "sdc1", "path": "/dev/fake-sdc1", "type": "part", "size": 31_000_000_000,
                  "fstype": "vfat", "label": self.boot_label, "mountpoints": ["/run/archiso/bootmnt"]}]},
            {"name": "loop0", "path": "/dev/loop0", "type": "loop", "size": 1000, "children": []},
        ]

    def smartctl_json(self, path):
        return self.smart.get(path)

    def which(self, name):
        return SHIM if name in ("ddrescue", "gddrescue") else shutil.which(name)

    def set_readonly(self, path):
        self.calls.append(("setro", path))
        return True, ""

    def used_bytes(self, part, scratch):
        return None  # tests never mount anything for real just to measure it

    def set_readwrite(self, path):
        self.calls.append(("setrw", path))
        return True, ""

    def umount(self, mountpoint):
        self.calls.append(("umount", mountpoint))
        return True, ""

    def mount(self, device, mountpoint, fstype=None, read_only=False):
        self.calls.append(("mount", device, mountpoint, fstype, read_only))
        os.makedirs(mountpoint, exist_ok=True)
        return True, ""

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
