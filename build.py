#!/usr/bin/env python3
"""Build DriveRescue-USB.zip: the two folders to copy onto the SystemRescue stick.

    python3 usb/build.py [output.zip]
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKIP_DIRS = {"__pycache__", ".pytest_cache"}


def add_bytes(zf, name, data, mode=0o644):
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.external_attr = (mode | 0o100000) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def add_file(zf, path, arcname, mode=0o644, lf=False):
    with open(path, "rb") as fh:
        data = fh.read()
    if lf:
        data = data.replace(b"\r\n", b"\n")
    add_bytes(zf, arcname, data, mode)


def build(out):
    with zipfile.ZipFile(out, "w") as zf:
        pkg = os.path.join(ROOT, "drive_rescue")
        for dirpath, dirs, files in os.walk(pkg):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
                add_file(zf, full, "drive-rescue/" + rel, lf=name.endswith((".py", ".html")))
        add_file(zf, os.path.join(HERE, "start.sh"), "drive-rescue/start.sh", 0o755, lf=True)
        add_file(zf, os.path.join(HERE, "README.txt"), "drive-rescue/README.txt", lf=False)
        add_file(zf, os.path.join(HERE, "autorun-src", "autorun"), "autorun/autorun", 0o755, lf=True)
    return out


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "DriveRescue-USB.zip")
    print("Built", build(dest))
