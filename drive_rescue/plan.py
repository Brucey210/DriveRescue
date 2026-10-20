"""The safety gate. Nothing is copied until a plan passes every check here."""
import json
import os
import re

from .drives import list_drives
from .util import human_size, slug

MOUNT_ROOT = os.environ.get("DRIVE_RESCUE_MOUNT_ROOT", "/mnt/drive-rescue")
FAT32_LIMIT = 4 * 1024**3 - 1
SPACE_MARGIN = 64 * 1024**2
DEFAULT_FOLDER = "DriveRescue"


def clean_folder(name):
    name = re.sub(r"[^A-Za-z0-9 _.-]", "", name or "").strip(" .")
    if not name or name in (".", ".."):
        return DEFAULT_FOLDER
    return name[:60]


def image_basename(drive):
    base = slug(drive.get("model") or drive.get("name"), "drive")
    serial = re.sub(r"[^A-Za-z0-9]", "", drive.get("serial") or "")
    if serial:
        base += "-" + serial[-4:].lower()
    return base


def explain_mount_error(message, fstype):
    low = (message or "").lower()
    if fstype == "ntfs" and any(w in low for w in ("hibernat", "fast restart", "unclean", "dirty", "read-only", "unsafe state")):
        return ("Windows left this drive in a 'not fully shut down' state (Fast Startup or hibernation). "
                "Start Windows, choose Shut down while holding the Shift key, then try again. "
                "Or choose a different healthy drive.")
    return f"The healthy drive could not be opened for writing. {message}".strip()


def find_drive(drives, path):
    for d in drives:
        if d["path"] == path:
            return d
    return None


def find_partition(drives, part_path):
    for d in drives:
        for p in d["partitions"]:
            if p["path"] == part_path:
                return d, p
    return None, None


def ensure_mounted_rw(system, part):
    """Return (mountpoint, error). Mounts the destination if it is not mounted."""
    for mp in part["mountpoints"]:
        if os.access(mp, os.W_OK):
            return mp, None
    mp = os.path.join(MOUNT_ROOT, part["name"])
    if part["mountpoints"]:
        return None, "The healthy drive is mounted read-only. Remount it as writable or pick another drive."
    ok, msg = system.mount(part["path"], mp, part["fstype"], read_only=False)
    if not ok:
        return None, explain_mount_error(msg, part["fstype"])
    if not os.access(mp, os.W_OK):
        return None, explain_mount_error("The drive opened read-only.", part["fstype"])
    return mp, None


def make_plan(system, source_path, dest_part_path, folder, opts=None, prepare=True):
    """Check everything. With prepare=True the destination is mounted and its folder created."""
    errors, warnings = [], []
    drives = list_drives(system)
    source = find_drive(drives, source_path)
    dest_drive, dest_part = find_partition(drives, dest_part_path)

    if source is None:
        errors.append("The failing drive was not found. Rescan and pick it again.")
    elif source["is_boot"]:
        errors.append("That is the rescue USB stick this program runs from. Pick the failing drive instead.")
    if dest_part is None:
        errors.append("The healthy drive was not found. Rescan and pick it again.")
    elif dest_drive["is_boot"]:
        errors.append("The rescue USB stick can't be used to save the copy. Pick the healthy drive.")
    if source and dest_drive and source["path"] == dest_drive["path"]:
        errors.append("The copy can't be saved on the failing drive itself. Pick a different drive.")

    result = {"ok": False, "errors": errors, "warnings": warnings, "source": source,
              "dest_drive": dest_drive, "dest_part": dest_part, "folder": clean_folder(folder)}
    if errors:
        return result

    if not dest_part["dest_ok"]:
        if dest_part["fstype"] == "vfat" and source["size"] <= FAT32_LIMIT:
            warnings.append("The healthy drive is FAT32, which works because the failing drive is under 4 GB.")
        else:
            errors.append(f"This partition can't be used: {dest_part['dest_reason']}. "
                          "Use a drive formatted as NTFS, exFAT or ext4.")
            return result

    if any(p["mountpoints"] for p in source["partitions"]):
        warnings.append("Parts of the failing drive are mounted. They will be released before copying.")

    mountpoint = None
    if prepare:
        mountpoint, err = ensure_mounted_rw(system, dest_part)
        if err:
            errors.append(err)
            return result
    else:
        mountpoint = next((m for m in dest_part["mountpoints"]), None)
        if mountpoint is None and dest_part["size"] < source["size"] + SPACE_MARGIN:
            # Not mounted yet, so free space can't be read, but a partition smaller than the failing
            # drive can never hold the copy. Say so now rather than after the drive is opened.
            errors.append(f"The healthy drive's partition ({dest_part['size_text']}) is smaller than the failing "
                          f"drive ({source['size_text']}), so the copy can't fit. Pick a bigger one.")
            return result

    base = image_basename(source)
    target_dir = os.path.join(mountpoint, result["folder"]) if mountpoint else None
    image = os.path.join(target_dir, base + ".img") if target_dir else None
    mapfile = os.path.join(target_dir, base + ".map") if target_dir else None
    jobfile = os.path.join(target_dir, base + ".job.json") if target_dir else None
    result.update({"mountpoint": mountpoint, "target_dir": target_dir, "image": image,
                   "mapfile": mapfile, "jobfile": jobfile})

    existing, resume = 0, False
    if image and os.path.exists(image):
        existing = os.path.getsize(image)
        resume = bool(mapfile and os.path.exists(mapfile))
        if resume and jobfile and os.path.exists(jobfile):
            try:
                with open(jobfile) as fh:
                    old = json.load(fh).get("source", {})
                if old.get("size") != source["size"] or (old.get("serial") and old.get("serial") != source["serial"]):
                    errors.append("That folder already holds a copy of a different drive. Choose another folder name.")
                    return result
            except (OSError, ValueError):
                pass
        if not resume:
            errors.append("A file with that name already exists in the folder but has no progress record. "
                          "Choose another folder name so nothing is overwritten.")
            return result

    need = max(0, source["size"] - existing) + SPACE_MARGIN
    free = None
    if mountpoint:
        try:
            free = system.free_bytes(mountpoint)
        except OSError as err:
            errors.append(f"Could not read the free space on the healthy drive: {err}")
            return result
        if free < need:
            errors.append(f"Not enough room on the healthy drive. The copy needs {human_size(need)} free "
                          f"and there is {human_size(free)}.")
            return result

    result.update({"need": need, "free": free, "resume": resume, "ok": True,
                   "need_text": human_size(need), "free_text": human_size(free) if free is not None else ""})
    return result
