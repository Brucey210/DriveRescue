"""Finding drives and judging their health."""
from .util import human_size

# Filesystems Linux can write to that are safe for one very large file.
GOOD_DEST_FS = {"ntfs", "exfat", "ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs"}
LIMITED_DEST_FS = {"vfat"}  # FAT32 cannot hold a file over 4 GB

TRANSPORT = {"sata": "SATA", "ata": "SATA", "usb": "USB", "nvme": "NVMe", "scsi": "SCSI", "sas": "SAS", "mmc": "SD card"}


def _mountpoints(dev):
    mps = dev.get("mountpoints")
    if mps is None and dev.get("mountpoint"):
        mps = [dev["mountpoint"]]
    return [m for m in (mps or []) if m]


def _is_boot_media(dev):
    """The rescue USB the program is running from must never be picked."""
    label = (dev.get("label") or "").upper()
    if label.startswith("RESCUE") or label.startswith("ARCHISO"):
        return True
    return any(m.startswith("/run/archiso") for m in _mountpoints(dev))


def _kind(dev):
    size = dev.get("size") or 0
    rota = dev.get("rota")
    rota = rota in (True, 1, "1")
    rm = dev.get("rm") in (True, 1, "1")
    if rota:
        return "Hard drive"
    if rm and size < 256 * 1000**3:
        return "USB stick"
    return "SSD"


def _dest_state(part):
    fs = (part.get("fstype") or "").lower()
    if fs in GOOD_DEST_FS:
        return True, ""
    if fs in LIMITED_DEST_FS:
        return False, "FAT32 cannot store a file larger than 4 GB"
    if not fs:
        return False, "No recognised filesystem"
    return False, f"{fs} is not supported for saving the copy"


def list_drives(system):
    drives = []
    for dev in system.lsblk():
        if dev.get("type") != "disk" or not (dev.get("size") or 0):
            continue
        parts = []
        boot = _is_boot_media(dev)
        for ch in dev.get("children") or []:
            if ch.get("type") not in ("part", None):
                continue
            ok, why = _dest_state(ch)
            boot = boot or _is_boot_media(ch)
            parts.append({
                "path": ch.get("path"),
                "name": ch.get("name"),
                "size": ch.get("size") or 0,
                "size_text": human_size(ch.get("size") or 0),
                "fstype": (ch.get("fstype") or ""),
                "label": ch.get("label") or "",
                "mountpoints": _mountpoints(ch),
                "dest_ok": ok,
                "dest_reason": why,
            })
        tran = (dev.get("tran") or "").lower()
        model = (dev.get("model") or "").strip()
        drives.append({
            "path": dev.get("path"),
            "name": model or "Unknown drive",
            "model": model,
            "serial": (dev.get("serial") or "").strip(),
            "size": dev.get("size") or 0,
            "size_text": human_size(dev.get("size") or 0),
            "kind": _kind(dev),
            "transport": TRANSPORT.get(tran, tran.upper() if tran else ""),
            "is_boot": boot,
            "partitions": parts,
        })
    return drives


def _attr(data, attr_id):
    for a in (data.get("ata_smart_attributes") or {}).get("table", []):
        if a.get("id") == attr_id:
            raw = a.get("raw") or {}
            return raw.get("value")
    return None


def check_health(system, path):
    """Quick read-only SMART look. Never raises; unknown is a valid answer."""
    data = system.smartctl_json(path)
    if not data:
        return {"status": "unknown", "label": "Unknown", "details": [
            {"k": "SMART data", "v": "Not available from this drive", "level": "warn"}]}
    details, bad, warn = [], False, False
    passed = (data.get("smart_status") or {}).get("passed")
    if passed is False:
        bad = True
    details.append({"k": "Drive's own verdict", "v": "Passed" if passed else ("Failed" if passed is False else "Not reported"),
                    "level": "bad" if passed is False else "ok"})
    for attr_id, name, is_bad in ((5, "Reallocated sectors", False), (197, "Waiting to be remapped", True),
                                  (198, "Unreadable sectors", True)):
        value = _attr(data, attr_id)
        if value is not None:
            level = "ok" if value == 0 else ("bad" if is_bad else "warn")
            bad = bad or (value > 0 and is_bad)
            warn = warn or (value > 0 and not is_bad)
            details.append({"k": name, "v": f"{value:,}", "level": level})
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        crit, media = nvme.get("critical_warning", 0), nvme.get("media_errors", 0)
        bad = bad or bool(crit) or media > 0
        details.append({"k": "Media errors", "v": f"{media:,}", "level": "bad" if media else "ok"})
    temp = (data.get("temperature") or {}).get("current")
    if temp is not None:
        details.append({"k": "Temperature", "v": f"{temp} °C", "level": "warn" if temp >= 55 else "ok"})
    if bad:
        return {"status": "failing", "label": "Failing", "details": details}
    if warn:
        return {"status": "warning", "label": "Worn", "details": details}
    return {"status": "healthy", "label": "Healthy", "details": details}
