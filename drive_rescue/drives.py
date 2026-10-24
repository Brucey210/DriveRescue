"""Finding drives and judging their health."""
import re

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


# SystemRescue's disc is labelled RESCUE plus its version digits (RESCUE1302), on a FAT32 stick or an
# ISO9660 disc. A person's own drive called "Rescue backup" must not be mistaken for it.
_BOOT_LABEL = re.compile(r"^(RESCUE\d{3,5}|ARCHISO.*)$", re.IGNORECASE)
_BOOT_FS = {"", "vfat", "iso9660", "udf"}


def _is_boot_media(dev):
    """The rescue USB the program is running from must never be picked."""
    label = (dev.get("label") or "").strip()
    if _BOOT_LABEL.match(label) and (dev.get("fstype") or "").lower() in _BOOT_FS:
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


def health_advice(status, details):
    """A plain sentence on what the health check means for the person. Never alarmist, never falsely calm."""
    def value(name):
        for d in details:
            if d["k"] == name:
                try:
                    return int(str(d["v"]).replace(",", ""))
                except ValueError:
                    return 0
        return 0
    if status == "failing":
        extra = ""
        if value("Unreadable sectors") or value("Waiting to be remapped"):
            extra = " It has parts it can no longer read."
        return ("This drive is failing." + extra + " Don't keep using it. Copy what you need now, "
                "then replace it. Every extra hour of use can cost you files.")
    if status == "warning":
        return ("This drive is wearing out. It still works, but it has started to fail in small ways. "
                "Copy your important files soon and plan to replace it.")
    if status == "healthy":
        return ("The drive itself reports no problems. If files still go missing or it is slow, the cause may be "
                "the cable, the USB port, or the file system rather than the drive. Cloning works well here.")
    return ("This drive didn't report its health, which is common with USB cases. That does not mean it is fine. "
            "Judge it by how it behaves: clicking, freezing or very slow means treat it as failing.")


def check_health(system, path):
    """Quick read-only SMART look. Never raises; unknown is a valid answer."""
    data = system.smartctl_json(path)
    if not data:
        det = [{"k": "SMART data", "v": "Not available from this drive", "level": "warn"}]
        return {"status": "unknown", "label": "Unknown", "details": det, "advice": health_advice("unknown", det)}
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
        return {"status": "failing", "label": "Failing", "details": details, "advice": health_advice("failing", details)}
    if warn:
        return {"status": "warning", "label": "Worn", "details": details, "advice": health_advice("warning", details)}
    return {"status": "healthy", "label": "Healthy", "details": details, "advice": health_advice("healthy", details)}


def drive_temperature(system, path):
    """The drive's own temperature in degrees, or None when it doesn't report one."""
    try:
        data = system.smartctl_json(path) or {}
    except Exception:
        return None
    temp = (data.get("temperature") or {}).get("current")
    return temp if isinstance(temp, (int, float)) else None
