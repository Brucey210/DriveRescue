#!/usr/bin/env python3
"""Make a pretend failing drive for safe practice and for testing. Needs root, losetup, dmsetup, mkfs.ext4.

    sudo python3 tools/damaged_drive.py create [--dir DIR] [--size-mb 96] [--name dr-practice]
    sudo python3 tools/damaged_drive.py remove  [--dir DIR] [--name dr-practice]

`create` makes a disk image holding an ext4 file system with a few files, then presents it as a drive
(/dev/mapper/NAME) where certain stretches give real read errors, like bad sectors do. It prints the
device and where the damage is. Point Drive Rescue at it, or use it in tests. Nothing real is touched.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

SECTOR = 512
MIB = 1024 * 1024


def run(*cmd, check=True, input_text=None):
    return subprocess.run(cmd, check=check, capture_output=True, text=True, input=input_text)


def build_table(loop, size_bytes, bad):
    """dmsetup table: good stretches map to the loop device, bad stretches are error targets."""
    lines, cursor = [], 0
    for start, length in sorted(bad):
        if start > cursor:
            lines.append(f"{cursor // SECTOR} {(start - cursor) // SECTOR} linear {loop} {cursor // SECTOR}")
        lines.append(f"{start // SECTOR} {length // SECTOR} error")
        cursor = start + length
    if cursor < size_bytes:
        lines.append(f"{cursor // SECTOR} {(size_bytes - cursor) // SECTOR} linear {loop} {cursor // SECTOR}")
    return "\n".join(lines) + "\n"


def create(workdir, size_mb=96, name="dr-practice", bad=None):
    os.makedirs(workdir, exist_ok=True)
    size = size_mb * MIB
    tree = os.path.join(workdir, "tree")
    for d in ("Pictures", "Documents"):
        os.makedirs(os.path.join(tree, d), exist_ok=True)
    # files of known content: the big ones are written first so they sit low on the disk, where the damage is
    with open(os.path.join(tree, "Pictures", "holiday-big.bin"), "wb") as fh:
        fh.write(b"B" * (8 * MIB))
    with open(os.path.join(tree, "Documents", "report.txt"), "w") as fh:
        fh.write("Quarterly report\n" * 200)
    with open(os.path.join(tree, "Pictures", "small.bin"), "wb") as fh:
        fh.write(b"S" * (256 * 1024))
    backing = os.path.join(workdir, "healthy.img")
    run("truncate", "-s", str(size), backing)
    run("mkfs.ext4", "-q", "-F", "-L", "Practice", "-d", tree, backing)
    loop = run("losetup", "--find", "--show", backing).stdout.strip()
    # damage: a stretch in the middle of the big file, found by asking the file system where it is
    first_extent = _first_extent_bytes(backing, "Pictures/holiday-big.bin")
    bad = bad or [(first_extent + 3 * MIB, 64 * 1024)]
    bad = [((s // SECTOR) * SECTOR, (n // SECTOR) * SECTOR) for s, n in bad]
    run("dmsetup", "create", name, input_text=build_table(loop, size, bad))
    info = {"device": f"/dev/mapper/{name}", "loop": loop, "backing": backing, "size": size, "bad": bad,
            "tree": tree, "big_file": "Pictures/holiday-big.bin", "name": name}
    with open(os.path.join(workdir, "damaged.json"), "w") as fh:
        json.dump(info, fh)
    return info


def _first_extent_bytes(image, path):
    """Where on the disk (in bytes from its start) the first piece of a file sits."""
    import re
    mount = tempfile_mount(image)
    try:
        out = run("filefrag", "-v", "-b1", os.path.join(mount, path)).stdout
    finally:
        run("umount", mount, check=False)
    for line in out.splitlines():
        m = re.match(r"^\s*\d+:\s+(\d+)\.\.\s*(\d+):\s+(\d+)\.\.\s*(\d+):\s+(\d+):", line)
        if m:
            return int(m.group(3))
    raise RuntimeError("could not find where the file sits on the disk")


def tempfile_mount(image):
    mp = tempfile.mkdtemp(prefix="dr-mnt-")
    run("mount", "-o", "ro,loop", image, mp)
    return mp


def remove(workdir, name="dr-practice"):
    run("dmsetup", "remove", "--retry", name, check=False)
    info_path = os.path.join(workdir, "damaged.json")
    if os.path.exists(info_path):
        info = json.load(open(info_path))
        run("losetup", "-d", info["loop"], check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("create", "remove"))
    ap.add_argument("--dir", default=os.path.join(tempfile.gettempdir(), "dr-practice"))
    ap.add_argument("--size-mb", type=int, default=96)
    ap.add_argument("--name", default="dr-practice")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run this as root (sudo).")
    if args.action == "create":
        info = create(args.dir, args.size_mb, args.name)
        print(f"Pretend failing drive: {info['device']}  ({info['size'] // MIB} MB)")
        for start, length in info["bad"]:
            print(f"  unreadable: {start} to {start + length} bytes")
        print(f"Remove it with: sudo python3 {sys.argv[0]} remove --dir {args.dir} --name {args.name}")
    else:
        remove(args.dir, args.name)
        print("Removed.")


if __name__ == "__main__":
    main()
