"""A quick look at the system before the screens open: is everything the program needs here?"""
import os
import sys

# (key, label, commands (any one will do), needed, what it is used for / what to do about it)
CHECKS = [
    ("ddrescue", "ddrescue", ("ddrescue", "gddrescue"), True, "Copies drives. Without it nothing can be copied."),
    ("losetup", "Disk tools", ("losetup", "blockdev", "mount", "umount"), True, "Locks and opens drives."),
    ("smartctl", "smartctl", ("smartctl",), False, "Reads drive health. Without it, health shows as Unknown."),
    ("photorec", "PhotoRec", ("photorec",), False, "Scans a copy for files by type. Without it, “Scan for files” is unavailable."),
    ("ntfsresize", "NTFS resizer", ("ntfsresize",), False, "Lets a cloned Windows partition use extra space."),
    ("ext", "ext4 tools", ("e2fsck", "resize2fs"), False, "Lets a cloned Linux partition use extra space."),
    ("browser", "Web browser", ("firefox", "firefox-esr", "chromium", "google-chrome"), False,
     "Shows these screens. If missing, open the address printed in the terminal from any browser."),
]


def run_checks(which=None, euid=None):
    """Return {'ok': bool, 'items': [...]}. `which` and `euid` can be replaced in tests."""
    if which is None:
        import shutil
        which = shutil.which
    euid = os.geteuid() if euid is None and hasattr(os, "geteuid") else (euid if euid is not None else 0)
    items = [{"key": "root", "label": "Administrator rights", "ok": euid == 0, "needed": True,
              "note": "Needed to read and write drives. Start the program as root."}]
    for key, label, cmds, needed, note in CHECKS:
        missing = [c for c in cmds if which(c) is None]
        ok = len(missing) < len(cmds) if key in ("ddrescue", "browser") else not missing
        items.append({"key": key, "label": label, "ok": ok, "needed": needed, "note": note,
                      "missing": missing if not ok else []})
    items.append({"key": "python", "label": "Python 3.8 or newer", "ok": sys.version_info >= (3, 8), "needed": True,
                  "note": "Runs the program."})
    return {"ok": all(i["ok"] for i in items if i["needed"]), "items": items}


def main():
    result = run_checks()
    for i in result["items"]:
        mark = "ok     " if i["ok"] else ("MISSING" if i["needed"] else "missing")
        print(f"[{mark}] {i['label']}" + ("" if i["ok"] else f" - {i['note']}"))
    print("\nReady." if result["ok"] else "\nSomething required is missing, see above.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
