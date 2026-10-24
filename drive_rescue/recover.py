"""Getting files back out of the copy.

The image is opened read-only. Files are browsed and copied out to a folder on
the healthy drive. If the filesystem inside the image is too damaged to browse,
a file scan (PhotoRec) can carve out files by type instead.
"""
import base64
import errno
import os
import subprocess
import threading

from . import mapfile as mapfile_mod
from .drives import _mountpoints
from .plan import MOUNT_ROOT
from .util import human_size

MAX_ENTRIES = 3000
PREVIEW_IMAGE_MAX = 6 * 1024 * 1024
PREVIEW_TEXT_BYTES = 8 * 1024
IMAGE_TYPES = {  # extension -> (mime, the bytes a real file of that kind starts with)
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)), "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)), "gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    "webp": ("image/webp", (b"RIFF",)), "bmp": ("image/bmp", (b"BM",)),
}
TEXT_EXTS = {"txt", "md", "csv", "log", "json", "xml", "ini", "cfg", "conf", "yml", "yaml", "tsv", "rtf", "srt"}


class RecoverError(Exception):
    pass


LOCKED_TYPES = {"crypto_LUKS": "LUKS", "BitLocker": "BitLocker", "bitlocker": "BitLocker"}


FILE_TYPES = {
    "photo": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif", "tif", "tiff", "raw", "cr2", "cr3", "nef",
              "arw", "dng", "orf", "rw2"},
    "document": {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "odt", "ods", "odp", "rtf", "md", "csv",
                 "pages", "numbers", "key", "epub"},
    "video": {"mp4", "mov", "avi", "mkv", "wmv", "m4v", "mpg", "mpeg", "3gp", "mts", "m2ts", "flv", "webm"},
    "audio": {"mp3", "wav", "flac", "m4a", "aac", "ogg", "wma", "aiff", "opus"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"},
}
_EXT_TYPE = {ext: kind for kind, exts in FILE_TYPES.items() for ext in exts}
THUMB_MAX = 1024 * 1024
SEARCH_LIMIT = 500
SEARCH_SECONDS = 10


def file_type(name):
    """photo, document, video, audio, archive, or "" for anything else."""
    return _EXT_TYPE.get(os.path.splitext(name)[1].lower().lstrip("."), "")


def exif_thumbnail(data):
    """The small preview picture most camera and phone JPEGs carry inside themselves, or None.
    Reading it needs only the first few KB of the file, so a folder of big photos stays quick."""
    try:
        if data[:2] != b"\xff\xd8":
            return None
        pos = 2
        while pos + 4 <= len(data) and data[pos] == 0xFF:
            marker, length = data[pos + 1], int.from_bytes(data[pos + 2:pos + 4], "big")
            if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\0\0":
                tiff = data[pos + 10:pos + 2 + length]
                end = "<" if tiff[:2] == b"II" else ">"
                if tiff[:2] not in (b"II", b"MM"):
                    return None
                u16 = lambda o: int.from_bytes(tiff[o:o + 2], "little" if end == "<" else "big")
                u32 = lambda o: int.from_bytes(tiff[o:o + 4], "little" if end == "<" else "big")
                ifd0 = u32(4)
                ifd1 = u32(ifd0 + 2 + 12 * u16(ifd0))  # the second directory describes the thumbnail
                if not ifd1 or ifd1 + 2 > len(tiff):
                    return None
                start = size = None
                for i in range(u16(ifd1)):
                    entry = ifd1 + 2 + 12 * i
                    tag = u16(entry)
                    if tag == 0x0201:
                        start = u32(entry + 8)
                    elif tag == 0x0202:
                        size = u32(entry + 8)
                if start is None or not size or start + size > len(tiff):
                    return None
                thumb = tiff[start:start + size]
                return thumb if thumb[:2] == b"\xff\xd8" else None
            if marker in (0xDA, 0xD9):
                return None
            pos += 2 + length
    except (IndexError, ValueError):
        return None
    return None


SECTOR = 512
SALVAGE_PIECE = 4096


def damage_table(segments, part_start, part_size, base_dev):
    """The device-mapper table that shows one partition of the copy with every unrecovered part
    turned into a read error. `segments` are the progress file's (position, size, status).
    Returns (table text, number of bytes that are not recovered). Good areas are trimmed inward and bad
    ones grow outward to whole sectors, so a bad byte can never be passed off as good."""
    end = part_start + part_size
    good = []
    for pos, size, status in sorted(segments):
        if status != "+":
            continue
        lo, hi = max(pos, part_start), min(pos + size, end)
        lo = -(-lo // SECTOR) * SECTOR
        hi = hi // SECTOR * SECTOR
        if hi > lo:
            if good and good[-1][1] >= lo:
                good[-1] = (good[-1][0], max(good[-1][1], hi))
            else:
                good.append((lo, hi))
    lines, cursor, bad = [], part_start, 0
    total_sectors = part_size // SECTOR
    for lo, hi in good + [(end - end % SECTOR if end % SECTOR else end, end - end % SECTOR if end % SECTOR else end)]:
        if lo > cursor:
            lines.append(("error", cursor, lo))
            bad += lo - cursor
        if hi > lo:
            lines.append(("linear", lo, hi))
        cursor = max(cursor, hi)
    out = []
    for kind, lo, hi in lines:
        start, length = (lo - part_start) // SECTOR, (hi - lo) // SECTOR
        if length <= 0 or start >= total_sectors:
            continue
        out.append(f"{start} {length} error" if kind == "error" else f"{start} {length} linear {base_dev} {start}")
    return "\n".join(out) + "\n", bad


def safe_join(root, rel):
    """Join rel under root, refusing any path that escapes it (including via symlinks)."""
    root_real = os.path.realpath(root)
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise RecoverError("Invalid path.")
    target = os.path.realpath(os.path.join(root_real, *parts))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise RecoverError("Invalid path.")
    return target


class Recovery:
    def __init__(self, system, image, out_dir, mapfile=None):
        self.system = system
        self.image = image
        self.out_dir = out_dir
        self.mapfile = mapfile
        self._dm = []
        self._crypt = []
        self.locked = []
        self.loop = None
        self.volumes = []
        self._mounts = []
        self._lock = threading.Lock()
        self._cancel = False
        self._proc = None
        self.task = {"kind": None, "state": "idle", "copied": 0, "bytes": 0, "failed": [],
                     "current": "", "message": "", "damaged": []}

    # ---- opening ---------------------------------------------------------
    def open(self):
        if self.volumes or self.loop:
            return self.describe()
        loop, err = self.system.attach_image(self.image)
        if not loop:
            raise RecoverError(f"The copy could not be opened: {err}")
        self.loop = loop
        devs = {d["path"]: d for d in self.system.lsblk() if d.get("path")}
        dev = devs.get(loop) or {"path": loop, "name": os.path.basename(loop)}
        candidates = list(dev.get("children") or [])
        if not candidates:  # a filesystem straight on the image, with no partition table
            candidates = [dev]
        for i, part in enumerate(candidates):
            fstype, label = part.get("fstype"), part.get("label")
            if not fstype:
                # lsblk only knows filesystem types while udev is running; blkid reads them directly
                found = self.system.probe(part.get("path") or loop)
                fstype, label = found.get("TYPE"), label or found.get("LABEL")
            if fstype in LOCKED_TYPES:
                device, checked, damaged, dm_name = self._checked_device(part, i)
                self.locked.append({"id": str(len(self.locked)), "name": label or part.get("name") or f"Section {i + 1}",
                                    "kind": LOCKED_TYPES[fstype], "size_text": human_size(part.get("size") or 0),
                                    "device": device, "index": i, "dm": dm_name, "damaged": damaged, "checked": checked})
                continue
            if not fstype or fstype in ("swap", "LVM2_member"):
                continue
            existing = _mountpoints(part)
            if existing:
                self._add_volume(part, fstype, label, i, existing[0], None, 0)
                continue
            device, checked, damaged, dm_name = self._checked_device(part, i)
            mp = os.path.join(MOUNT_ROOT, "image-%d" % i)
            ok, _msg = self.system.mount(device, mp, fstype, read_only=True)
            if not ok and dm_name:
                self._drop_dm(dm_name)  # could not open it with the damage check; open it plainly
                checked, damaged = False, 0
                ok, _msg = self.system.mount(part["path"], mp, fstype, read_only=True)
            if not ok:
                continue
            self._mounts.append(mp)
            self._add_volume(part, fstype, label, i, mp, checked, damaged)
        return self.describe()

    def _add_volume(self, part, fstype, label, index, mountpoint, checked, damaged, size=None):
        self.volumes.append({
            "id": str(len(self.volumes)),
            "name": (label or part.get("name") or f"Volume {index + 1}"),
            "fstype": fstype,
            "size_text": human_size(size if size is not None else part.get("size") or 0),
            "mountpoint": mountpoint,
            "damage_check": checked,
            "damaged_bytes": damaged,
        })

    def unlock(self, locked_id, password):
        """Open a protected (BitLocker or LUKS) section with its password, read-only. The password goes
        straight to the unlocking tool and is never stored or logged."""
        item = next((x for x in self.locked if x["id"] == str(locked_id)), None)
        if item is None:
            raise RecoverError("That section is not locked.")
        if not password:
            raise RecoverError("Type the password first.")
        name = f"dr-crypt-{os.getpid()}-{item['index']}"
        dev, err = self.system.crypt_open(item["device"], name, password, item["kind"])
        if not dev:
            raise RecoverError(err or "That password didn't work.")
        self._crypt.append(name)
        found = self.system.probe(dev)
        fstype, label = found.get("TYPE"), found.get("LABEL")
        if not fstype or fstype in LOCKED_TYPES:
            self._drop_crypt(name)
            raise RecoverError("The section opened, but nothing readable was inside it.")
        mp = os.path.join(MOUNT_ROOT, "image-crypt-%d" % item["index"])
        ok, _msg = self.system.mount(dev, mp, fstype, read_only=True)
        if not ok:
            self._drop_crypt(name)
            raise RecoverError("The password worked, but the files inside could not be opened.")
        self._mounts.append(mp)
        self._add_volume({"name": item["name"]}, fstype, label or item["name"], item["index"], mp,
                         item["checked"], item["damaged"], size=0)
        self.volumes[-1]["size_text"] = item["size_text"]
        self.locked = [x for x in self.locked if x is not item]
        return self.describe()

    def _checked_device(self, part, index):
        """The device to open for this partition. When parts of the copy were never recovered it is a
        virtual disk where those parts give read errors, so damaged files are noticed instead of
        coming out with blanks in them. Returns (device, checked, bytes not recovered, virtual disk name);
        checked is None when there was nothing to check, True when it works, False when it could not be done."""
        if not self.mapfile or not os.path.exists(self.mapfile):
            return part["path"], None, 0, None
        try:
            with open(self.mapfile) as fh:
                mf = mapfile_mod.parse_mapfile(fh.read())
        except OSError:
            return part["path"], False, 0, None
        start = self.system.partition_offset(part["path"]) if part["path"] != self.loop else 0
        size = part.get("size") or 0
        if not size or not mf.segments:
            return part["path"], False, 0, None
        table, bad = damage_table(mf.segments, start, size, part["path"])
        if bad == 0:
            return part["path"], True, 0, None
        name = f"dr-image-{os.getpid()}-{index}"
        dev, _err = self.system.dm_create(name, table)
        if not dev:
            return part["path"], False, 0, None
        self._dm.append(name)
        return dev, True, bad, name

    def _drop_dm(self, only=None):
        for name in reversed(list(self._dm)):
            if only is None or name == only:
                self.system.dm_remove(name)
                self._dm.remove(name)

    def _drop_crypt(self, only=None):
        for name in reversed(list(self._crypt)):
            if only is None or name == only:
                self.system.crypt_close(name)
                self._crypt.remove(name)

    def describe(self):
        return {"volumes": [{k: v for k, v in vol.items() if k != "mountpoint"} for vol in self.volumes],
                "readable": bool(self.volumes),
                "unchecked": any(v.get("damage_check") is False for v in self.volumes),
                "locked": [{k: x[k] for k in ("id", "name", "kind", "size_text")} for x in self.locked]}

    def close(self):
        for mp in reversed(self._mounts):
            self.system.umount(mp)
        self._mounts = []
        self._drop_crypt()
        self._drop_dm()
        self.locked = []
        if self.loop:
            self.system.detach_image(self.loop)
        self.loop, self.volumes = None, []

    # ---- browsing --------------------------------------------------------
    def _volume(self, volume_id):
        for vol in self.volumes:
            if vol["id"] == str(volume_id):
                return vol
        raise RecoverError("That volume is not open.")

    def list_dir(self, volume_id, rel=""):
        vol = self._volume(volume_id)
        path = safe_join(vol["mountpoint"], rel)
        entries = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        size = 0 if is_dir else e.stat(follow_symlinks=False).st_size
                    except OSError:
                        is_dir, size = False, 0
                    entries.append({"name": e.name, "is_dir": is_dir, "size": size, "size_text": human_size(size),
                                    "type": "" if is_dir else file_type(e.name),
                                    "rel": (rel.strip("/") + "/" + e.name).lstrip("/")})
                    if len(entries) >= MAX_ENTRIES:
                        break
        except OSError as err:
            raise RecoverError("This folder is too damaged to read." if err.errno == errno.EIO else
                               f"This folder can't be read: {err.strerror}")
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"rel": rel.strip("/"), "entries": entries, "truncated": len(entries) >= MAX_ENTRIES}

    # ---- finding files ---------------------------------------------------
    def search(self, volume_id, query="", kind="", clock=None):
        """Look through the whole volume for files by part of the name and/or by type.
        Stops after a short while or enough results, and says so, rather than hanging on a damaged drive."""
        import time
        clock = clock or time.monotonic
        vol = self._volume(volume_id)
        root = vol["mountpoint"]
        needle = (query or "").strip().lower()
        if kind and kind not in FILE_TYPES:
            raise RecoverError("Unknown kind of file.")
        if not needle and not kind:
            raise RecoverError("Type a name, or choose a kind of file.")
        results, stack, truncated = [], [""], False
        deadline = clock() + SEARCH_SECONDS
        while stack and not truncated:
            rel = stack.pop()
            try:
                it = os.scandir(os.path.join(root, rel) if rel else root)
            except OSError:
                continue
            with it:
                try:
                    for e in it:
                        if clock() > deadline or len(results) >= SEARCH_LIMIT:
                            truncated = True
                            break
                        try:
                            if e.is_symlink():
                                continue
                            is_dir = e.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        child = (rel + "/" + e.name) if rel else e.name
                        if is_dir:
                            stack.append(child)
                            continue
                        kind_of = file_type(e.name)
                        if kind and kind_of != kind:
                            continue
                        if needle and needle not in e.name.lower():
                            continue
                        try:
                            size = e.stat(follow_symlinks=False).st_size
                        except OSError:
                            size = 0
                        results.append({"name": e.name, "is_dir": False, "size": size, "size_text": human_size(size),
                                        "type": kind_of, "rel": child, "folder": rel})
                except OSError:
                    continue
        results.sort(key=lambda x: (x["name"].lower(), x["rel"]))
        return {"entries": results, "truncated": truncated, "query": query, "kind": kind}

    def thumbnail(self, volume_id, rel):
        """A small picture for the file list: the photo's own built-in preview, or the whole picture when
        it is small. Returns {} when there is nothing cheap to show."""
        vol = self._volume(volume_id)
        path = safe_join(vol["mountpoint"], rel)
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext not in IMAGE_TYPES or os.path.islink(path) or not os.path.isfile(path):
            return {}
        mime, magics = IMAGE_TYPES[ext]
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                head = fh.read(65536)
                if not head.startswith(magics):
                    return {}
                if ext in ("jpg", "jpeg"):
                    thumb = exif_thumbnail(head)
                    if thumb:
                        return {"mime": "image/jpeg", "data": base64.b64encode(thumb).decode("ascii")}
                if size > THUMB_MAX:
                    return {}
                data = head + fh.read(THUMB_MAX)
        except OSError:
            return {}
        return {"mime": mime, "data": base64.b64encode(data[:THUMB_MAX]).decode("ascii")}

    # ---- looking before recovering -----------------------------------------
    def preview(self, volume_id, rel):
        """A look at one file without copying it: a picture, the start of a text file, or just its size.
        Never follows links, and never reads more than a few MB."""
        vol = self._volume(volume_id)
        path = safe_join(vol["mountpoint"], rel)
        if os.path.islink(path) or not os.path.isfile(path):
            raise RecoverError("There is nothing to preview here.")
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        try:
            size = os.path.getsize(path)
            if ext in IMAGE_TYPES:
                mime, magics = IMAGE_TYPES[ext]
                if size > PREVIEW_IMAGE_MAX:
                    return {"kind": "none", "size_text": human_size(size), "message": "This picture is too large to preview."}
                with open(path, "rb") as fh:
                    data = fh.read(PREVIEW_IMAGE_MAX + 1)
                if not data.startswith(magics):
                    return {"kind": "none", "size_text": human_size(size),
                            "message": "This file is damaged, so it can't be shown."}
                return {"kind": "image", "mime": mime, "size_text": human_size(size),
                        "data": base64.b64encode(data).decode("ascii")}
            if ext in TEXT_EXTS:
                with open(path, "rb") as fh:
                    head = fh.read(PREVIEW_TEXT_BYTES)
                if b"\0" in head:
                    return {"kind": "none", "size_text": human_size(size), "message": "This file can't be shown as text."}
                return {"kind": "text", "size_text": human_size(size), "truncated": size > PREVIEW_TEXT_BYTES,
                        "text": head.decode("utf-8", "replace")}
            return {"kind": "none", "size_text": human_size(size), "message": "There is no preview for this kind of file."}
        except OSError as err:
            if err.errno == errno.EIO:
                return {"kind": "none", "size_text": "", "message": "This file is in a damaged area and can't be read."}
            raise RecoverError(f"This file can't be read: {err.strerror}")

    def estimate(self, items, max_files=200_000, max_seconds=15.0):
        """Total size in bytes of the chosen files and folders, or None when it takes too long to count."""
        import time as _time
        deadline, total, seen = _time.monotonic() + max_seconds, 0, 0
        for item in items:
            try:
                vol = self._volume(item.get("volume"))
                path = safe_join(vol["mountpoint"], item.get("rel", ""))
            except RecoverError:
                continue
            if os.path.islink(path):
                continue
            if os.path.isfile(path):
                total += os.path.getsize(path)
                continue
            for dirpath, _dirs, files in os.walk(path, onerror=lambda e: None):
                for name in files:
                    seen += 1
                    if seen > max_files or _time.monotonic() > deadline:
                        return None
                    try:
                        full = os.path.join(dirpath, name)
                        if not os.path.islink(full):
                            total += os.path.getsize(full)
                    except OSError:
                        pass
        return total

    # ---- copying out -----------------------------------------------------
    def start_copy(self, items, out_dir=None):
        with self._lock:
            if self.task["state"] == "running":
                raise RecoverError("Another recovery is already running.")
            if out_dir:
                self.out_dir = out_dir
            self._cancel = False
            self.task = {"kind": "copy", "state": "running", "copied": 0, "bytes": 0, "failed": [],
                         "current": "", "message": "", "damaged": []}
        threading.Thread(target=self._copy_worker, args=(items,), daemon=True).start()

    def _copy_one(self, src, dst):
        """Copy one file. Where part of it sits in a damaged area, the readable parts are still saved
        and the unreadable pieces are filled with blanks, and the file is listed as damaged so nobody
        mistakes it for a good one. A file that cannot be read at all is not kept."""
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        bad = 0
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            fd, size, pos = fin.fileno(), os.fstat(fin.fileno()).st_size, 0
            while pos < size:
                if self._cancel:
                    raise RecoverError("cancelled")
                want = min(1 << 20, size - pos)
                try:
                    block = os.pread(fd, want, pos)
                except OSError as err:
                    if err.errno != errno.EIO:
                        raise
                    block, lost = self._salvage(fd, pos, want)
                    bad += lost
                if not block:
                    break
                fout.write(block)
                pos += len(block)
                self.task["bytes"] += len(block)
        if bad and bad >= size:
            os.remove(dst)
            raise OSError(errno.EIO, "unreadable")
        try:
            os.utime(dst, (os.stat(src).st_atime, os.stat(src).st_mtime))
        except OSError:
            pass
        self.task["copied"] += 1
        if bad:
            self.task["damaged"].append({"path": src, "bad_bytes": bad, "bad_text": human_size(bad)})

    @staticmethod
    def _salvage(fd, pos, want):
        """Read a stretch piece by piece. Returns (data, bytes that could not be read)."""
        out, lost = [], 0
        end = pos + want
        while pos < end:
            n = min(SALVAGE_PIECE, end - pos)
            try:
                piece = os.pread(fd, n, pos)
            except OSError as err:
                if err.errno != errno.EIO:
                    raise
                piece = b"\0" * n
                lost += n
            if not piece:
                break
            out.append(piece)
            pos += len(piece)
        return b"".join(out), lost

    def _copy_worker(self, items):
        try:
            for item in items:
                vol = self._volume(item.get("volume"))
                try:
                    src = safe_join(vol["mountpoint"], item.get("rel", ""))
                except RecoverError:
                    # Links and paths that leave the copy are never followed. Skip that one and keep going.
                    self.task["failed"].append({"path": item.get("rel", ""),
                                                "reason": "skipped (a link, which is never followed)"})
                    continue
                rel = item.get("rel", "").strip("/")
                base = os.path.join(self.out_dir, "recovered", vol["name"].replace("/", "_"))
                if os.path.isdir(src) and not os.path.islink(src):
                    for dirpath, _dirs, files in os.walk(src, onerror=lambda e: self._fail(e.filename, e)):
                        for name in files:
                            self._copy_path(os.path.join(dirpath, name), vol["mountpoint"], base)
                else:
                    self._copy_path(src, vol["mountpoint"], base)
            self._set("done", "")
        except RecoverError as err:
            self._set("cancelled" if str(err) == "cancelled" else "error", str(err))
        except OSError as err:
            msg = "The healthy drive is full." if err.errno == errno.ENOSPC else f"Stopped: {err.strerror}"
            self._set("error", msg)

    def _copy_path(self, src, root, base):
        if os.path.islink(src):
            return
        self.task["current"] = os.path.basename(src)
        dst = os.path.join(base, os.path.relpath(src, root))
        try:
            self._copy_one(src, dst)
        except RecoverError:
            raise
        except OSError as err:
            if err.errno == errno.ENOSPC:
                raise
            self._fail(src, err)

    def _fail(self, path, err):
        reason = "unreadable (damaged area)" if getattr(err, "errno", None) == errno.EIO else str(err)
        self.task["failed"].append({"path": path, "reason": reason})

    def _set(self, state, message):
        with self._lock:
            self.task["state"] = state
            self.task["message"] = message

    # ---- scanning for files ----------------------------------------------
    def start_carve(self, out_dir=None):
        tool = self.system.which("photorec")
        if not tool:
            raise RecoverError("The file scanner (PhotoRec) is not available on this system.")
        with self._lock:
            if self.task["state"] == "running":
                raise RecoverError("Another recovery is already running.")
            if out_dir:
                self.out_dir = out_dir
            self._cancel = False
            outdir = os.path.join(self.out_dir, "found-files")
            os.makedirs(outdir, exist_ok=True)
            self.task = {"kind": "carve", "state": "running", "copied": 0, "bytes": 0, "failed": [],
                         "current": "", "message": "", "outdir": outdir, "damaged": []}
        cmd = [tool, "/d", outdir + os.sep, "/cmd", self.image, "partition_none,fileopt,everything,enable,wholespace,search"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        threading.Thread(target=self._carve_worker, daemon=True).start()

    def _carve_worker(self):
        rc = self._proc.wait()
        with self._lock:
            if self._cancel:
                self.task["state"] = "cancelled"
            else:
                self.task["state"] = "done" if rc == 0 else "error"
                if rc != 0:
                    self.task["message"] = f"The file scan stopped with an error (code {rc})."

    def _count_found(self):
        outdir = self.task.get("outdir")
        total = 0
        if outdir and os.path.isdir(outdir):
            for _d, _dirs, files in os.walk(outdir):
                total += len(files)
        return total

    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def status(self):
        t = dict(self.task)
        if t.get("kind") == "carve":
            t["copied"] = self._count_found()
        t["bytes_text"] = human_size(t.get("bytes", 0))
        t["damaged_count"] = len(t.get("damaged", []))
        t["damaged"] = t.get("damaged", [])[:50]
        t["failed_count"] = len(t.get("failed", []))
        t["failed"] = t.get("failed", [])[:50]
        t["out_dir"] = os.path.join(self.out_dir, "found-files" if t.get("kind") == "carve" else "recovered")
        return t
