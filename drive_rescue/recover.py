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
    def __init__(self, system, image, out_dir):
        self.system = system
        self.image = image
        self.out_dir = out_dir
        self.loop = None
        self.volumes = []
        self._mounts = []
        self._lock = threading.Lock()
        self._cancel = False
        self._proc = None
        self.task = {"kind": None, "state": "idle", "copied": 0, "bytes": 0, "failed": [],
                     "current": "", "message": ""}

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
            if not fstype or fstype in ("swap", "LVM2_member", "crypto_LUKS"):
                continue
            existing = _mountpoints(part)
            if existing:
                mp = existing[0]
            else:
                mp = os.path.join(MOUNT_ROOT, "image-%d" % i)
                ok, _msg = self.system.mount(part["path"], mp, fstype, read_only=True)
                if not ok:
                    continue
                self._mounts.append(mp)
            self.volumes.append({
                "id": str(len(self.volumes)),
                "name": (label or part.get("name") or f"Volume {i + 1}"),
                "fstype": fstype,
                "size_text": human_size(part.get("size") or 0),
                "mountpoint": mp,
            })
        return self.describe()

    def describe(self):
        return {"volumes": [{k: v for k, v in vol.items() if k != "mountpoint"} for vol in self.volumes],
                "readable": bool(self.volumes)}

    def close(self):
        for mp in reversed(self._mounts):
            self.system.umount(mp)
        self._mounts = []
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
                                    "rel": (rel.strip("/") + "/" + e.name).lstrip("/")})
                    if len(entries) >= MAX_ENTRIES:
                        break
        except OSError as err:
            raise RecoverError("This folder is too damaged to read." if err.errno == errno.EIO else
                               f"This folder can't be read: {err.strerror}")
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"rel": rel.strip("/"), "entries": entries, "truncated": len(entries) >= MAX_ENTRIES}

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

    # ---- copying out -----------------------------------------------------
    def start_copy(self, items):
        with self._lock:
            if self.task["state"] == "running":
                raise RecoverError("Another recovery is already running.")
            self._cancel = False
            self.task = {"kind": "copy", "state": "running", "copied": 0, "bytes": 0, "failed": [],
                         "current": "", "message": ""}
        threading.Thread(target=self._copy_worker, args=(items,), daemon=True).start()

    def _copy_one(self, src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                if self._cancel:
                    raise RecoverError("cancelled")
                block = fin.read(1 << 20)
                if not block:
                    break
                fout.write(block)
                self.task["bytes"] += len(block)
        try:
            os.utime(dst, (os.stat(src).st_atime, os.stat(src).st_mtime))
        except OSError:
            pass
        self.task["copied"] += 1

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
    def start_carve(self):
        tool = self.system.which("photorec")
        if not tool:
            raise RecoverError("The file scanner (PhotoRec) is not available on this system.")
        with self._lock:
            if self.task["state"] == "running":
                raise RecoverError("Another recovery is already running.")
            self._cancel = False
            outdir = os.path.join(self.out_dir, "found-files")
            os.makedirs(outdir, exist_ok=True)
            self.task = {"kind": "carve", "state": "running", "copied": 0, "bytes": 0, "failed": [],
                         "current": "", "message": "", "outdir": outdir}
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
        t["failed_count"] = len(t.get("failed", []))
        t["failed"] = t.get("failed", [])[:50]
        t["out_dir"] = os.path.join(self.out_dir, "found-files" if t.get("kind") == "carve" else "recovered")
        return t
