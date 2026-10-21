"""Cloning a healthy drive onto a bigger one, then using the extra room.

This is the one mode that writes to a whole drive, so it has its own safety gate:
  * the drive being copied must not be failing (a failing drive belongs in "Rescue files");
  * the target must be at least as big, have the same sector size, and not be the USB stick;
  * the target is erased, so the person has to type ERASE;
  * afterwards the partition table is extended so the whole target is usable.
"""
import os
import re
import time

from . import ddrescue, partitions
from .drives import check_health, list_drives
from .job import ImageJob
from .plan import MOUNT_ROOT, find_drive
from .util import human_size, slug

WORK_DIR = os.environ.get("DRIVE_RESCUE_WORK", "/tmp/drive-rescue")
CONFIRM_WORD = "ERASE"
SAMPLE = 1024 * 1024
CLONE_PHASES = [
    ("copy", "Copy the drive", "Copying everything, sector by sector"),
    ("layout", "Use the extra space", "Extending the partitions to fill the new drive"),
]


def build_clone_command(binary, source, dest, mapfile, phase, opts, sector=512):
    """ddrescue needs -f to write onto a whole drive. Only this mode ever passes it."""
    cmd = [binary, "-f", "-d"] + ddrescue.sector_args(sector)
    if phase == "quick":
        cmd += ["-n"]
    elif phase == "retry":
        cmd += ["-r", str(int(opts.get("retries", 1)))]
    else:
        raise ValueError(f"unknown phase {phase!r}")
    return cmd + [source, dest, mapfile]


def clean_clone_opts(raw):
    raw = raw if isinstance(raw, dict) else {}
    try:
        retries = int(raw.get("retries", 1))
    except (TypeError, ValueError):
        retries = 1
    return {"retries": max(0, min(5, retries)), "style": "fast", "direction": "forward"}


def _fstypes_from_drive(drive, layout):
    """Match table entries to the filesystem types Linux already reports for the source."""
    out, spare = {}, list(drive["partitions"])
    for p in layout["parts"]:
        size = partitions.part_bytes(layout, p)
        for ch in spare:
            if ch["size"] == size:
                out[p["slot"]] = ch["fstype"] or None
                spare.remove(ch)
                break
    return out


def _label_for(drive, layout, slot):
    p = next(x for x in layout["parts"] if x["slot"] == slot)
    size = partitions.part_bytes(layout, p)
    for ch in drive["partitions"]:
        if ch["size"] == size:
            return ch["label"] or ch["name"]
    return p.get("name") or f"Partition {slot + 1}"


def boot_check(layout, firmware=None):
    """Will a computer start up from this drive? Judged from the partition table, which a clone keeps.
    `firmware` is how the computer running this program started ("uefi" or "bios"), if known."""
    kind = layout.get("kind")
    if kind in (None, "none"):
        return {"mode": "unknown", "level": "info",
                "text": "There is no partition table to read, so it can't be said whether this drive starts up a computer."}
    parts = layout.get("parts") or []
    if kind == "gpt":
        if any(p["type"] == partitions.GPT_ESP for p in parts):
            note = ("It has a UEFI boot partition, so it starts up on any computer that boots in UEFI mode "
                    "(almost every PC made in the last ten years).")
            if firmware == "bios":
                note += " The computer you are using now started in older BIOS mode, so check your firmware settings if you test the clone here."
            return {"mode": "uefi", "level": "ok", "text": note}
        if any(p["type"] == partitions.GPT_BIOS_BOOT for p in parts):
            return {"mode": "legacy", "level": "ok",
                    "text": "It has a legacy BIOS boot area, so it starts up on computers in BIOS (legacy) mode."}
        return {"mode": "data", "level": "info",
                "text": "It has no boot partition, so it is a data drive. It holds your files but won't start up a computer."}
    if any(p.get("active") for p in parts):
        note = ("One partition is marked bootable, so it starts up on computers in BIOS (legacy / CSM) mode. "
                "Modern UEFI-only computers can't start from it.")
        if firmware == "uefi":
            note += " The computer you are using now started in UEFI mode, so turn on Legacy/CSM in its firmware settings to test the clone here."
        return {"mode": "legacy", "level": "warn" if firmware == "uefi" else "ok", "text": note}
    return {"mode": "data", "level": "info",
            "text": "No partition is marked bootable, so it is a data drive. It holds your files but won't start up a computer."}


def used_estimate(system, source):
    """Total bytes of files on the source's partitions, or None if any of them can't be measured."""
    total = 0
    scratch = os.path.join(MOUNT_ROOT, "clone-measure")
    for part in source["partitions"]:
        if not part.get("fstype"):
            continue
        used = system.used_bytes(part, scratch)
        if used is None:
            return None
        total += used
    return total


def shrink_check(system, source, dest):
    """The target is smaller than the source. Would the files at least fit? This does not shrink
    anything: it tells the person whether shrinking the main partition first would make a clone possible."""
    used = used_estimate(system, source)
    if used is None:
        return {"known": False, "fits": False, "used_text": "", "text": ""}
    room = int(dest["size"] * 0.97) - 4 * 1024 * 1024  # headroom for the table, alignment and filesystem overhead
    fits = used <= room
    return {"known": True, "fits": fits, "used_text": human_size(used), "text": (
        f"The files on this drive only use about {human_size(used)}, which would fit on the {dest['size_text']} target. "
        "To clone onto a smaller drive, first shrink the main partition on the original drive to a smaller size "
        "(GParted on this stick, or Disk Management in Windows), then come back and clone it."
        if fits else
        f"The files on this drive use about {human_size(used)}, which is more than the {dest['size_text']} target "
        "can hold. Pick a bigger target, or remove files first.")}


def preview_layout(system, source, dest_size, sector):
    """What will happen to the partitions on a drive of dest_size. Reads the source table only."""
    try:
        layout = partitions.read_layout(source["path"], sector)
    except OSError as err:
        return {"known": False, "extends": False, "text": f"Could not read the partition table: {err}"}
    if layout["kind"] == "none":
        return {"known": False, "extends": False, "kind": "none", "boot": boot_check(layout, system.firmware()),
                "text": "No partition table was found on this drive. It will be copied as it is and the extra "
                        "room will stay unused."}
    fstypes = _fstypes_from_drive(source, layout)
    plan = partitions.plan_growth(layout, dest_size // sector, fstypes)
    info = {"known": True, "kind": layout["kind"], "extends": plan["extends"], "reason": plan["reason"],
            "unallocated": plan.get("unallocated_sectors", 0) * sector,
            "boot": boot_check(layout, system.firmware())}
    if plan["extends"]:
        slot, new_end = plan["grow"]
        p = next(x for x in layout["parts"] if x["slot"] == slot)
        info["grow"] = {"name": _label_for(source, layout, slot), "from_text": human_size(partitions.part_bytes(layout, p)),
                        "to_text": human_size((new_end - p["start"] + 1) * sector)}
        info["text"] = (f"“{info['grow']['name']}” will grow from {info['grow']['from_text']} to "
                        f"{info['grow']['to_text']}, so the whole new drive is usable.")
        if plan["move"]:
            info["text"] += " A small recovery partition at the end is moved to the new end of the drive."
        if plan["reason"]:
            info["text"] += " " + plan["reason"]
    else:
        info["text"] = (plan["reason"] or "Nothing needs to change.")
    return info


def make_clone_plan(system, source_path, dest_path):
    errors, warnings = [], []
    drives = list_drives(system)
    source, dest = find_drive(drives, source_path), find_drive(drives, dest_path)
    result = {"ok": False, "mode": "clone", "errors": errors, "warnings": warnings, "source": source,
              "dest_drive": dest, "health": None, "layout": None, "dest_contents": []}
    if source is None:
        errors.append("The drive to copy was not found. Rescan and pick it again.")
    elif source["is_boot"]:
        errors.append("That is the rescue USB stick this program runs from. Pick another drive.")
    if dest is None:
        errors.append("The target drive was not found. Rescan and pick it again.")
    elif dest["is_boot"]:
        errors.append("The rescue USB stick can't be the target. Pick the bigger drive.")
    if source and dest and source["path"] == dest["path"]:
        errors.append("The drive can't be copied onto itself. Pick a different target.")
    if errors:
        return result

    if dest["size"] < source["size"]:
        errors.append(f"The target is smaller than the drive being copied "
                      f"({dest['size_text']} against {source['size_text']}). It must be the same size or bigger.")
        result["shrink"] = shrink_check(system, source, dest)
        if result["shrink"]["text"]:
            errors.append(result["shrink"]["text"])
    elif dest["size"] == source["size"]:
        warnings.append("The target is exactly the same size, so there is no extra room to gain.")
    ssz, dsz = system.sector_size(source["path"]), system.sector_size(dest["path"])
    if ssz != dsz:
        errors.append(f"The two drives use different sector sizes ({ssz} and {dsz} bytes). "
                      "A copy would not start up, so this pair can't be cloned.")

    health = check_health(system, source["path"])
    result["health"] = health
    if health["status"] == "failing":
        errors.append("This drive is reporting signs of failing. Cloning is only for healthy drives. "
                      "Use “Rescue files” instead, which is built to cope with read errors.")
    elif health["status"] == "warning":
        warnings.append("The drive shows some wear (remapped sectors). It can still be cloned, but keep the original until you have checked the copy.")
    elif health["status"] == "unknown":
        warnings.append("The drive doesn't report its health, so it could not be checked. If it has been acting up, use “Rescue files” instead.")

    unreadable = _read_test(source, system)
    if unreadable:
        errors.append("Parts of this drive could not be read in a quick test. Use “Rescue files” instead, "
                      "which keeps going past read errors.")

    result["dest_contents"] = [{"name": p["label"] or p["name"], "fstype": p["fstype"] or "unknown", "size_text": p["size_text"]}
                               for p in dest["partitions"]]
    if any(p["mountpoints"] for p in source["partitions"] + dest["partitions"]):
        warnings.append("Some parts of these drives are in use. They will be released before copying.")
    if not errors:
        result["layout"] = preview_layout(system, source, dest["size"], ssz)
        if result["layout"]["known"] and not result["layout"]["extends"] and dest["size"] > source["size"]:
            warnings.append("The extra room can't be added to a partition automatically. " + result["layout"]["reason"]
                            + " The copy is still made, and the space can be added afterwards in Windows Disk Management or GParted.")
        elif not result["layout"]["known"] and dest["size"] > source["size"]:
            warnings.append(result["layout"]["text"])
    base = f"{slug(source['model'] or source['name'], 'drive')}-to-{slug(dest['model'] or dest['name'], 'drive')}"
    ident = re.sub(r"[^A-Za-z0-9]", "", (source["serial"] or "") + (dest["serial"] or ""))[-12:]
    mapfile = os.path.join(WORK_DIR, f"clone-{base}-{ident or 'x'}.map")
    result.update({"image": dest["path"], "mapfile": mapfile, "target_dir": WORK_DIR, "jobfile": None,
                   "folder": "", "sector": ssz, "ok": not errors,
                   "need_text": human_size(source["size"]), "free_text": dest["size_text"], "resume": False})
    return result


def _read_test(source, system):
    """Read a little from the start, middle and end of the drive. True means it failed."""
    size = source["size"]
    try:
        with open(source["path"], "rb") as fh:
            for off in (0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)):
                fh.seek(off)
                if not fh.read(SAMPLE):
                    return True
    except OSError:
        return True
    return False


# ------------------------------------------------------------------------ after the copy
def resize_filesystem(system, dest, sector, part, fstype):
    """Grow the filesystem to fill its (already extended) partition. Returns (ok, message)."""
    offset, size = part["start"] * sector, (part["end"] - part["start"] + 1) * sector
    loop, err = system.attach_region(dest, offset, size)
    if not loop:
        return False, f"Could not open the partition to resize it: {err}"
    try:
        if fstype == "ntfs":
            rc, out, err = system.run(["ntfsresize", "--no-progress-bar", loop], timeout=7200, input_text="y\n")
            if rc != 0:
                return False, ("Windows left this drive in a 'not fully shut down' state, so the Windows partition was "
                               "enlarged but its filesystem was not. Start Windows and use Extend Volume in Disk Management."
                               if any(w in (out + err).lower() for w in ("hibernat", "dirty", "scheduled", "unclean", "inconsistent"))
                               else f"The Windows partition was enlarged but its filesystem could not be: {(err or out).strip()[:200]}")
            return True, "Windows may run a disk check the first time it starts. That is normal."
        if fstype in ("ext2", "ext3", "ext4"):
            rc, out, err = system.run(["e2fsck", "-f", "-p", loop], timeout=7200)
            if rc > 1:
                return False, f"The filesystem needs a repair before it can grow: {(out + err).strip()[-200:]}"
            rc, out, err = system.run(["resize2fs", loop], timeout=7200)
            return (rc == 0, "" if rc == 0 else f"Could not grow the filesystem: {(err or out).strip()[:200]}")
        if fstype in ("btrfs", "xfs"):
            mp = os.path.join(MOUNT_ROOT, "clone-resize")
            ok, msg = system.mount(loop, mp, fstype, read_only=False)
            if not ok:
                return False, f"Could not open the filesystem to grow it: {msg}"
            try:
                cmd = ["btrfs", "filesystem", "resize", "max", mp] if fstype == "btrfs" else ["xfs_growfs", mp]
                rc, out, err = system.run(cmd, timeout=3600)
            finally:
                system.umount(mp)
            return (rc == 0, "" if rc == 0 else f"Could not grow the filesystem: {(err or out).strip()[:200]}")
        return False, "This kind of filesystem can't be grown automatically."
    finally:
        system.detach_image(loop)


def finish_layout(system, dest, progress=None):
    """Make the whole target drive usable. Returns a result dict for the screens."""
    res = {"ok": True, "extended": False, "lines": [], "warnings": [], "unallocated_text": "", "fs_ok": None}
    sector = system.sector_size(dest)
    system.run(["sync"], timeout=600)
    layout = partitions.read_layout(dest, sector)
    total = layout["disk_sectors"]
    if layout["kind"] == "none":
        res["lines"].append("The copy has no partition table, so nothing was extended.")
        res["warnings"].append(layout["note"])
        return res
    fstypes = {}
    for p in layout["parts"]:
        loop, _ = system.attach_region(dest, p["start"] * sector, (p["end"] - p["start"] + 1) * sector)
        if loop:
            try:
                fstypes[p["slot"]] = (system.probe(loop).get("TYPE") or "").lower() or None
            finally:
                system.detach_image(loop)
    plan = partitions.plan_growth(layout, total, fstypes)
    if not plan["ok"]:
        res["warnings"].append(plan["reason"])
        return res
    new = partitions.apply_plan(dest, layout, plan, progress=progress)
    problems = partitions.verify(dest, layout, plan)
    if problems:
        res["ok"] = False
        res["warnings"].append("The new partition table did not check out: " + " ".join(problems))
        return res
    res["lines"].append("The partition table now covers the whole drive.")
    if plan["grow"]:
        slot, new_end = plan["grow"]
        part = next(p for p in new["parts"] if p["slot"] == slot)
        old = next(p for p in layout["parts"] if p["slot"] == slot)
        res["extended"] = True
        res["from_text"] = human_size(partitions.part_bytes(layout, old))
        res["to_text"] = human_size(partitions.part_bytes(new, part))
        res["lines"].append(f"The main partition grew from {res['from_text']} to {res['to_text']}.")
        ok, msg = resize_filesystem(system, dest, sector, part, fstypes.get(slot) or "")
        res["fs_ok"] = ok
        if ok:
            res["lines"].append("Its filesystem was grown to match.")
            if msg:
                res["lines"].append(msg)
        else:
            res["warnings"].append(msg)
    if plan["move"]:
        res["lines"].append("The small recovery partition now sits at the end of the drive.")
    last = max(p["end"] for p in new["parts"]) if new["parts"] else 0
    free = max(0, partitions.usable_last(new, total) - last) * sector
    res["unallocated_text"] = human_size(free) if free > 2 * 1024 * 1024 else ""
    res["boot"] = boot_check(new, system.firmware())
    if plan.get("reason"):
        res["warnings"].append(plan["reason"])
    system.run(["blockdev", "--rereadpt", dest], timeout=30)
    return res


class CloneJob(ImageJob):
    """Copy one whole drive onto another with ddrescue, then extend the partitions."""

    def __init__(self, system, plan, opts, **kw):
        opts = {"retries": 1, **(opts or {})}
        super().__init__(system, plan, opts, **kw)
        self.result = None
        self.finishing_note = ""

    def _command(self, key):
        return build_clone_command(self.binary, self.plan["source"]["path"], self.plan["image"],
                                   self.plan["mapfile"], key, self.opts, sector=self.plan.get("sector", 512))

    def _prepare(self):
        src, dst = self.plan["source"], self.plan["dest_drive"]
        ok, err = self.system.set_readonly(src["path"])
        if not ok:
            self.message = f"Could not lock the source drive as read-only ({err}); copying reads only, never writes."
        # A drive locked earlier in this session (as a failing source) would refuse the copy. The target
        # has passed every check and the person has confirmed it will be erased.
        self.system.set_readwrite(dst["path"])
        for drive in (src, dst):
            for part in drive["partitions"]:
                for mp in part["mountpoints"]:
                    done, err = self.system.umount(mp)
                    if not done:
                        from .job import JobError
                        raise JobError(f"Could not release {mp}: {err}")
        os.makedirs(WORK_DIR, exist_ok=True)
        try:
            os.remove(self.plan["mapfile"])  # a clone always starts clean, never from another pair's progress
        except FileNotFoundError:
            pass

    def _write_jobfile(self, state):
        pass

    def _complete(self):
        sums = self._summary()
        if sums["finished"] < self.size:
            self.result = {"ok": False, "extended": False, "lines": [], "unallocated_text": "", "fs_ok": None,
                           "warnings": [f"{human_size(self.size - sums['finished'])} of the drive could not be read, so the copy is "
                                        "incomplete and the partitions were left as they were. Use “Rescue files” for this drive."]}
            self._finish("done", "")
            return
        with self._lock:
            self.state = "finishing"
            self.message = "Making the whole drive usable…"
        try:
            self.result = finish_layout(self.system, self.plan["image"])
        except Exception as exc:  # the copy itself is fine; say what did not happen
            self.result = {"ok": False, "extended": False, "lines": [], "unallocated_text": "", "fs_ok": None,
                           "warnings": [f"The copy finished but the extra room could not be set up: {exc}"]}
        self._finish("done", "")

    def snapshot(self):
        snap = super().snapshot()
        st = snap["state"]
        copy = "done" if st in ("finishing", "done") else ("active" if st in ("running", "pausing", "paused", "error") else "waiting")
        lay = "active" if st == "finishing" else ("done" if st == "done" and self.result and self.result.get("ok") and
                                                  snap["complete"] else ("skipped" if st == "done" else "waiting"))
        snap["phases"] = [{"key": k, "title": t, "note": n, "status": s}
                          for (k, t, n), s in zip(CLONE_PHASES, (copy, lay))]
        snap.update({"mode": "clone", "result": self.result, "outlook": None, "image": self.plan["image"],
                     "dest_name": self.plan["dest_drive"]["name"], "dest_size_text": self.plan["dest_drive"]["size_text"]})
        return snap
