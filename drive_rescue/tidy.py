"""Tidying what a file scan found: sort by kind, set aside tiny scraps and exact duplicates.

Nothing is ever deleted. Scraps and duplicates are moved into their own folders so the person can look.
"""
import hashlib
import os
import shutil

GROUPS = {
    "Pictures": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif", "cr2", "cr3", "nef", "arw",
                 "dng", "orf", "rw2", "raf", "psd", "svg", "ico"},
    "Documents": {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "rtf", "odt", "ods", "odp", "csv", "html",
                  "htm", "xml", "pages", "numbers", "key", "epub", "mobi", "wps", "msg", "eml", "pst"},
    "Videos": {"mp4", "mov", "avi", "mkv", "wmv", "mpg", "mpeg", "3gp", "flv", "webm", "m4v", "mts", "m2ts", "vob"},
    "Audio": {"mp3", "wav", "flac", "m4a", "aac", "ogg", "wma", "aif", "aiff", "mid", "midi", "opus"},
    "Archives": {"zip", "rar", "7z", "gz", "tar", "bz2", "xz", "iso", "cab"},
}
TINY = 512
SET_ASIDE = ("_tiny-scraps", "_duplicates")


def group_of(name):
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    for group, exts in GROUPS.items():
        if ext in exts:
            return group, ext
    return "Other", ext or "no-extension"


def _hash(path, limit=None):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _move(src, folder, name):
    os.makedirs(folder, exist_ok=True)
    dst, n = os.path.join(folder, name), 1
    stem, ext = os.path.splitext(name)
    while os.path.exists(dst):
        dst = os.path.join(folder, f"{stem}_{n}{ext}")
        n += 1
    shutil.move(src, dst)
    return dst


def organize(outdir, cancelled=lambda: False):
    """Sort the scan's files. Returns {"groups": {name: count}, "tiny": n, "duplicates": n}."""
    files = []
    for dirpath, dirs, names in os.walk(outdir):
        dirs[:] = [d for d in dirs if d not in SET_ASIDE and d not in GROUPS and d != "Other"]
        for name in names:
            files.append(os.path.join(dirpath, name))
    summary = {"groups": {}, "tiny": 0, "duplicates": 0}
    seen = {}  # (size, hash) -> kept path
    by_size = {}
    for path in files:
        try:
            by_size.setdefault(os.path.getsize(path), []).append(path)
        except OSError:
            continue
    for size, paths in sorted(by_size.items()):
        paths = sorted(paths)  # the same answer every time, whatever order the disk lists things in
        for path in paths:
            if cancelled():
                return summary
            name = os.path.basename(path)
            if size < TINY:
                _move(path, os.path.join(outdir, "_tiny-scraps"), name)
                summary["tiny"] += 1
                continue
            if len(paths) > 1:
                try:
                    key = (size, _hash(path))
                except OSError:
                    key = None
                if key and key in seen:
                    _move(path, os.path.join(outdir, "_duplicates"), name)
                    summary["duplicates"] += 1
                    continue
                if key:
                    seen[key] = path
            group, ext = group_of(name)
            _move(path, os.path.join(outdir, group, ext), name)
            summary["groups"][group] = summary["groups"].get(group, 0) + 1
    for dirpath, dirs, names in os.walk(outdir, topdown=False):  # remove the scan's now-empty folders
        if dirpath != outdir and not os.listdir(dirpath):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    return summary
