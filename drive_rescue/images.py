"""Finding drive copies (image files) that were saved earlier, so they can be opened again."""
import json
import os

from . import mapfile
from .util import human_size

SKIP_DIRS = {"$recycle.bin", "system volume information", "windows", "program files", "program files (x86)",
             "programdata", "proc", "sys", "dev", "lost+found", "node_modules", ".git"}
MIN_SIZE = 1024 * 1024
MAX_DEPTH = 3
MAX_FOUND = 100


def scan(root, max_depth=MAX_DEPTH, limit=MAX_FOUND):
    """Image files (*.img) under `root`, newest first. Never follows links; skips system folders."""
    found = []
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: None):
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))] \
            if depth < max_depth else []
        for name in files:
            if not name.lower().endswith(".img"):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size < MIN_SIZE or not os.path.isfile(path):
                continue
            found.append((st.st_mtime, path, st.st_size))
    found.sort(reverse=True)
    return [describe(path, size, mtime) for mtime, path, size in found[:limit]]


def describe(path, size, mtime):
    stem = os.path.splitext(path)[0]
    item = {"path": path, "name": os.path.basename(path), "folder": os.path.basename(os.path.dirname(path)),
            "size": size, "size_text": human_size(size), "mtime": int(mtime),
            "mapfile": stem + ".map" if os.path.exists(stem + ".map") else None,
            "model": "", "percent": None}
    try:
        with open(stem + ".job.json") as fh:
            job = json.load(fh)
        src = job.get("source") or {}
        item["model"] = src.get("model") or ""
        item["serial"] = src.get("serial") or ""
    except (OSError, ValueError):
        pass
    if item["mapfile"]:
        try:
            with open(item["mapfile"]) as fh:
                sums = mapfile.summarize(mapfile.parse_mapfile(fh.read()), size)
            item["percent"] = round(100.0 * sums["finished"] / size, 1) if size else None
        except OSError:
            pass
    return item
