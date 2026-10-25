"""Checking a finished copy against the drive it came from, without reading the whole drive again."""
import os

ALIGN = 4096


def finished_ranges(segments):
    return sorted((pos, size) for pos, size, st in segments if st == "+" and size > 0)


def pick_samples(segments, count, chunk):
    """Places spread evenly over everything that was copied. Returns [(position, length)]."""
    ranges = finished_ranges(segments)
    total = sum(size for _pos, size in ranges)
    if not total or count <= 0:
        return []
    out = []
    for k in range(count):
        target = int((k + 0.5) * total / count)
        seen = 0
        for pos, size in ranges:
            if target < seen + size:
                start = pos + (target - seen)
                start = max(pos, start // ALIGN * ALIGN)
                length = min(chunk, pos + size - start)
                if length > 0 and (start, length) not in out:
                    out.append((start, length))
                break
            seen += size
    return out


def _read_fresh(path, pos, length):
    """Read straight from the file or drive, not from memory the system still holds from earlier."""
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        return os.pread(fd, length, pos)
    finally:
        os.close(fd)


def sample_check(source, image, segments, count=48, chunk=1024 * 1024, cancelled=lambda: False, progress=None):
    """Compare spread-out pieces of the copy with the original.
    {"checked": pieces compared, "bytes": ..., "different": [positions], "unreadable": pieces the
    original would not give again (not counted as a difference), "ok": bool}"""
    try:
        os.sync()
    except OSError:
        pass
    result = {"checked": 0, "bytes": 0, "different": [], "unreadable": 0, "ok": True}
    samples = pick_samples(segments, count, chunk)
    for i, (pos, length) in enumerate(samples):
        if cancelled():
            break
        try:
            a = _read_fresh(source, pos, length)
        except OSError:
            result["unreadable"] += 1
            continue
        try:
            b = _read_fresh(image, pos, length)
        except OSError:
            result["different"].append(pos)
            continue
        result["checked"] += 1
        result["bytes"] += length
        if a != b:
            result["different"].append(pos)
        if progress:
            progress(i + 1, len(samples))
    result["ok"] = not result["different"]
    return result
