"""Reading and extending partition tables (GPT and MBR), in plain Python.

After a small drive is cloned onto a bigger one, the copy still describes the small drive: the
partitions stop where the small drive stopped and the rest is unallocated. This module finds the
partition that should take the extra room, moves a small trailing recovery partition out of its
way when needed, and rewrites the table so the whole drive is usable.

Only the *destination* is ever written. Everything here works on a path, so tests can use
ordinary files as drives.
"""
import os
import struct
import uuid
import zlib

ALIGN = 2048  # sectors (1 MiB at 512 bytes)
GROWABLE_FS = {"ntfs", "ext2", "ext3", "ext4", "btrfs", "xfs"}
MAX_MOVE_BYTES = 8 * 1024**3  # only a small recovery partition is ever moved

GPT_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
GPT_ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MBR_RECOVERY = 0x27
MBR_EXTENDED = (0x05, 0x0F, 0x85)
MBR_MAX_SECTORS = 0xFFFFFFFF


class TableError(Exception):
    pass


# --------------------------------------------------------------------------- reading
def _read(path, offset, n):
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(n)


def _write(path, offset, data):
    with open(path, "r+b") as fh:
        fh.seek(offset)
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def disk_sectors_of(path, sector):
    with open(path, "rb") as fh:
        return fh.seek(0, os.SEEK_END) // sector


def _gpt_header(raw):
    """Parse one 92-byte GPT header, or None when it is not a valid one."""
    if len(raw) < 92 or raw[:8] != b"EFI PART":
        return None
    size = struct.unpack_from("<I", raw, 12)[0]
    if size < 92 or size > len(raw):
        return None
    crc = struct.unpack_from("<I", raw, 16)[0]
    body = bytearray(raw[:size])
    body[16:20] = b"\0\0\0\0"
    if zlib.crc32(bytes(body)) & 0xFFFFFFFF != crc:
        return None
    (rev, _sz, _crc, _res, my, alt, first, last) = struct.unpack_from("<IIIIQQQQ", raw, 8)
    guid = raw[56:72]
    entries_lba, num, esize, ecrc = struct.unpack_from("<QIII", raw, 72)
    if esize < 128 or esize % 8 or num == 0 or num > 4096:
        return None
    return {"size": size, "my": my, "alt": alt, "first": first, "last": last, "guid": guid,
            "entries_lba": entries_lba, "num": num, "esize": esize, "ecrc": ecrc}


def read_layout(path, sector=512):
    """Describe the partition table on `path`. Never raises for a table it does not understand."""
    total = disk_sectors_of(path, sector)
    layout = {"kind": "none", "sector": sector, "disk_sectors": total, "parts": [], "note": ""}
    if total < 64:
        layout["note"] = "The drive is too small to hold a partition table."
        return layout
    mbr = _read(path, 0, sector)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xAA":
        layout["note"] = "No partition table was found."
        return layout
    entries = [mbr[446 + 16 * i:462 + 16 * i] for i in range(4)]
    if entries[0][4] == 0xEE:
        hdr = _gpt_header(_read(path, sector, sector))
        from_backup = False
        if hdr is None:
            hdr = _gpt_header(_read(path, (total - 1) * sector, sector))
            from_backup = hdr is not None
        if hdr is None:
            layout["note"] = "The GPT partition table is damaged."
            return layout
        raw = _read(path, hdr["entries_lba"] * sector, hdr["num"] * hdr["esize"])
        if len(raw) < hdr["num"] * hdr["esize"] or zlib.crc32(raw) & 0xFFFFFFFF != hdr["ecrc"]:
            layout["note"] = "The GPT partition list is damaged."
            return layout
        parts = []
        for i in range(hdr["num"]):
            e = raw[i * hdr["esize"]:(i + 1) * hdr["esize"]]
            if not any(e[:16]):
                continue
            start, end = struct.unpack_from("<QQ", e, 32)
            parts.append({"slot": i, "start": start, "end": end,
                          "type": str(uuid.UUID(bytes_le=e[:16])),
                          "name": e[56:128].decode("utf-16le", "replace").rstrip("\0")})
        layout.update({"kind": "gpt", "hdr": hdr, "raw_entries": raw, "parts": parts,
                       "from_backup": from_backup})
        parts.sort(key=lambda p: p["start"])
        return layout
    parts = []
    for i, e in enumerate(entries):
        ptype = e[4]
        start, count = struct.unpack_from("<II", e, 8)
        if ptype and count:
            parts.append({"slot": i, "start": start, "end": start + count - 1, "type": ptype, "name": ""})
    parts.sort(key=lambda p: p["start"])
    layout.update({"kind": "mbr", "parts": parts, "mbr": mbr})
    return layout


def part_bytes(layout, p):
    return (p["end"] - p["start"] + 1) * layout["sector"]


def is_recovery(p, kind):
    return p["type"] == GPT_RECOVERY if kind == "gpt" else p["type"] == MBR_RECOVERY


def entries_sectors(layout):
    h = layout["hdr"]
    return -(-(h["num"] * h["esize"]) // layout["sector"])


def usable_last(layout, disk_sectors):
    if layout["kind"] == "gpt":
        return disk_sectors - 1 - entries_sectors(layout) - 1
    return min(disk_sectors - 1, MBR_MAX_SECTORS - 1)


def check_layout(layout):
    """Return a list of problems that make the table unsafe to edit."""
    problems = []
    parts = sorted(layout["parts"], key=lambda p: p["start"])
    for a, b in zip(parts, parts[1:]):
        if b["start"] <= a["end"]:
            problems.append("Two partitions overlap.")
    for p in parts:
        if p["end"] < p["start"]:
            problems.append("A partition has an invalid size.")
    if layout["kind"] == "mbr" and any(p["type"] in MBR_EXTENDED for p in parts):
        problems.append("The drive uses extended partitions, which are not resized automatically.")
    return problems


# --------------------------------------------------------------------------- planning
def plan_growth(layout, new_disk_sectors, fstypes):
    """Decide how the partitions should change on a drive of `new_disk_sectors`.

    fstypes maps a partition's slot to its filesystem name (or None when unknown).
    Returns a dict with ok, reason, grow (slot, new_end), move (slot, new_start), unallocated.
    """
    plan = {"ok": False, "reason": "", "grow": None, "move": None, "relocate_gpt": False,
            "disk_sectors": new_disk_sectors, "extends": False}
    if layout["kind"] == "none":
        plan["reason"] = layout["note"] or "The partition table is not recognised."
        return plan
    problems = check_layout(layout)
    if problems:
        plan["reason"] = problems[0]
        return plan
    parts = sorted(layout["parts"], key=lambda p: p["start"])
    limit = usable_last(layout, new_disk_sectors)
    if layout["kind"] == "gpt":
        plan["relocate_gpt"] = layout["hdr"]["alt"] != new_disk_sectors - 1 or layout["hdr"]["last"] != limit
        if parts and parts[0]["start"] < layout["hdr"]["first"]:
            plan["reason"] = "A partition starts before the usable area."
            return plan
    if not parts:
        plan["ok"], plan["reason"] = True, "The drive has no partitions."
        return plan
    if parts[-1]["end"] > limit:
        plan["reason"] = "A partition already reaches past the end of this drive."
        return plan
    plan["ok"] = True
    last = parts[-1]
    fs = lambda p: (fstypes.get(p["slot"]) or "").lower()
    target = move = None
    if is_recovery(last, layout["kind"]) and len(parts) >= 2 and part_bytes(layout, last) <= MAX_MOVE_BYTES:
        before = parts[-2]
        if fs(before) in GROWABLE_FS and not is_recovery(before, layout["kind"]):
            size = last["end"] - last["start"] + 1
            new_start = ((limit - size + 1) // ALIGN) * ALIGN
            if new_start > last["end"]:  # the new place must not touch the old one
                target, move = before, (last["slot"], new_start)
    elif fs(last) in GROWABLE_FS and not is_recovery(last, layout["kind"]):
        target = last
    if target is not None:
        new_end = (move[1] - 1) if move else limit
        if new_end > target["end"]:
            plan["grow"] = (target["slot"], new_end)
            plan["move"] = move
            plan["extends"] = True
    tail_end = (move[1] + (last["end"] - last["start"])) if move else None
    final_end = tail_end if tail_end is not None else (plan["grow"][1] if plan["grow"] else last["end"])
    plan["unallocated_sectors"] = max(0, limit - final_end)
    if not plan["extends"] and plan["unallocated_sectors"] > ALIGN:
        if layout["kind"] == "mbr" and new_disk_sectors - 1 > MBR_MAX_SECTORS:
            plan["reason"] = "An MBR partition table can't address more than 2 TB."
        else:
            plan["reason"] = ("The last partition's filesystem can't be extended automatically, "
                              "so the extra room is left unused.")
    elif plan["extends"] and layout["kind"] == "mbr" and new_disk_sectors - 1 > MBR_MAX_SECTORS:
        plan["reason"] = "An MBR partition table can't address more than 2 TB, so the rest is left unused."
    return plan


# --------------------------------------------------------------------------- writing
def _build_header(layout, my, alt, entries_lba, first, last, ecrc):
    h = layout["hdr"]
    raw = bytearray(layout["sector"])
    raw[:8] = b"EFI PART"
    struct.pack_into("<IIIIQQQQ", raw, 8, 0x00010000, h["size"], 0, 0, my, alt, first, last)
    raw[56:72] = h["guid"]
    struct.pack_into("<QIII", raw, 72, entries_lba, h["num"], h["esize"], ecrc)
    crc = zlib.crc32(bytes(raw[:h["size"]])) & 0xFFFFFFFF
    struct.pack_into("<I", raw, 16, crc)
    return bytes(raw)


def write_gpt(path, layout, parts, new_disk_sectors):
    """Write both copies of a GPT sized for `new_disk_sectors`, with the given partitions."""
    s, h = layout["sector"], layout["hdr"]
    esz = entries_sectors(layout)
    last = new_disk_sectors - 1 - esz - 1
    raw = bytearray(layout["raw_entries"])
    for p in parts:
        off = p["slot"] * h["esize"]
        struct.pack_into("<QQ", raw, off + 32, p["start"], p["end"])
    raw = bytes(raw)
    ecrc = zlib.crc32(raw) & 0xFFFFFFFF
    backup_entries_lba = new_disk_sectors - 1 - esz
    first = h["first"]
    # When the table was read from the backup copy (the main one was damaged), the entries address in
    # the header is the backup's, near the old end of the drive. The main copy always goes at LBA 2.
    primary_entries_lba = 2 if layout.get("from_backup") else h["entries_lba"]
    pad = lambda b: b + b"\0" * (esz * s - len(b))
    _write(path, backup_entries_lba * s, pad(raw))
    _write(path, (new_disk_sectors - 1) * s,
           _build_header(layout, new_disk_sectors - 1, 1, backup_entries_lba, first, last, ecrc))
    _write(path, primary_entries_lba * s, pad(raw))
    _write(path, s, _build_header(layout, 1, new_disk_sectors - 1, primary_entries_lba, first, last, ecrc))
    mbr = bytearray(_read(path, 0, s))
    if mbr[510:512] == b"\x55\xAA" and mbr[450] == 0xEE:  # keep the protective entry covering the disk
        struct.pack_into("<I", mbr, 446 + 12, min(new_disk_sectors - 1, 0xFFFFFFFF))
        _write(path, 0, bytes(mbr))


def write_mbr(path, layout, parts):
    mbr = bytearray(layout["mbr"])
    for p in parts:
        off = 446 + 16 * p["slot"]
        struct.pack_into("<II", mbr, off + 8, p["start"], p["end"] - p["start"] + 1)
    _write(path, 0, bytes(mbr))


def move_data(path, sector, old_start, new_start, sectors, chunk=4 * 1024 * 1024, progress=None):
    """Copy a partition's sectors to a new place that does not overlap, then check the copy."""
    if new_start <= old_start + sectors - 1 and new_start + sectors - 1 >= old_start:
        raise TableError("The new place for the partition overlaps its old place.")
    total = sectors * sector
    done = 0
    with open(path, "r+b") as fh:
        while done < total:
            n = min(chunk, total - done)
            fh.seek(old_start * sector + done)
            block = fh.read(n)
            if len(block) != n:
                raise TableError("Could not read the partition that needs to move.")
            fh.seek(new_start * sector + done)
            fh.write(block)
            done += n
            if progress:
                progress(done, total)
        fh.flush()
        os.fsync(fh.fileno())
        done = 0
        while done < total:
            n = min(chunk, total - done)
            fh.seek(old_start * sector + done)
            a = fh.read(n)
            fh.seek(new_start * sector + done)
            if a != fh.read(n):
                raise TableError("The moved partition did not copy correctly.")
            done += n


def apply_plan(path, layout, plan, progress=None):
    """Carry out a plan from plan_growth. Returns the new layout."""
    s = layout["sector"]
    new_total = plan["disk_sectors"]
    parts = [dict(p) for p in layout["parts"]]
    by_slot = {p["slot"]: p for p in parts}
    if plan["move"]:
        slot, new_start = plan["move"]
        p = by_slot[slot]
        size = p["end"] - p["start"] + 1
        if layout["kind"] == "gpt":  # make the table valid for the whole drive first
            write_gpt(path, layout, layout["parts"], new_total)
        move_data(path, s, p["start"], new_start, size, progress=progress)
        p["start"], p["end"] = new_start, new_start + size - 1
    if plan["grow"]:
        slot, new_end = plan["grow"]
        by_slot[slot]["end"] = new_end
    if layout["kind"] == "gpt":
        write_gpt(path, layout, parts, new_total)
    else:
        write_mbr(path, layout, parts)
    return read_layout(path, s)


def verify(path, layout_before, plan):
    """Check the table now on disk. Returns a list of problems (empty means all good)."""
    s = layout_before["sector"]
    now = read_layout(path, s)
    bad = []
    if now["kind"] != layout_before["kind"]:
        return ["The partition table could not be read back."]
    bad += check_layout(now)
    if len(now["parts"]) != len(layout_before["parts"]):
        bad.append("The number of partitions changed.")
    if now["kind"] == "gpt":
        total = plan["disk_sectors"]
        backup = _gpt_header(_read(path, (total - 1) * s, s))
        if backup is None:
            bad.append("The backup partition table is missing at the end of the drive.")
        if now["hdr"]["alt"] != total - 1 or now.get("from_backup"):
            bad.append("The partition table is not set for the full size of the drive.")
        for p in now["parts"]:
            if p["end"] > now["hdr"]["last"]:
                bad.append("A partition goes past the usable end of the drive.")
    return bad
