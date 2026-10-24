"""A quick read test: read a few small spots spread over a drive and judge how it responds."""
import mmap
import os
import threading
import time

SPOTS = 40
SPOT_BYTES = 64 * 1024
SLOW_SECONDS = 1.0
TIME_LIMIT = 25.0


def real_reader(path, size):
    """Returns read(offset) -> bytes, reading straight from the drive (not from memory)."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
    try:
        fd = os.open(path, flags)
        direct = bool(getattr(os, "O_DIRECT", 0))
    except OSError:
        fd, direct = os.open(path, os.O_RDONLY), False
    buf = mmap.mmap(-1, SPOT_BYTES)

    def read(offset):
        if direct:
            try:
                n = os.preadv(fd, [buf], offset)
                return n
            except OSError as err:
                if err.errno != 22:  # 22 = this drive refuses direct reads; use the ordinary way below
                    raise
        data = os.pread(fd, SPOT_BYTES, offset)
        try:
            os.posix_fadvise(fd, offset, SPOT_BYTES, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        return len(data)
    read.close = lambda: os.close(fd)
    return read


def spots(size, count=SPOTS):
    if size <= SPOT_BYTES:
        return [0]
    step = (size - SPOT_BYTES) // max(1, count - 1)
    return [min(size - SPOT_BYTES, (i * step) // 4096 * 4096) for i in range(count)]


def rate(errors, slow, hung, total):
    bad = errors + hung
    if bad == 0 and slow == 0:
        return "good"
    if bad == 0 and slow / total <= 0.1:
        return "fair"
    if (bad + slow) / total <= 0.4:
        return "poor"
    return "bad"


TEXT = {
    "good": "Every spot read quickly with no errors. The drive looks healthy right now. If you are losing files, "
            "the problem may be the file system or the cable.",
    "fair": "Mostly fine, with a few slow spots. The drive is starting to struggle. Copy your files soon.",
    "poor": "A good share of the drive was slow or unreadable. Expect a long copy and some files that can't be "
            "recovered. Don't keep using it.",
    "bad": "Most of the drive was slow or unreadable. Recovery is still worth trying, but expect it to take a long "
           "time and for some files to be lost. If the files are very valuable, consider a professional "
           "recovery service, and avoid running the drive more than necessary.",
}


def read_test(path, size, reader=None, count=SPOTS, time_limit=TIME_LIMIT, clock=time.monotonic):
    """Reads a spread of small spots. A read that never returns is counted as stuck, and never holds
    the test up for longer than `time_limit`. Returns a summary the screens can show."""
    reader = reader or real_reader(path, size)
    places = spots(size, count)
    out = {"errors": 0, "slow": 0, "hung": 0, "done": 0, "total": len(places)}
    lock = threading.Lock()

    def work():
        for off in places:
            t0 = clock()
            try:
                reader(off)
                took = clock() - t0
                with lock:
                    out["done"] += 1
                    if took > SLOW_SECONDS:
                        out["slow"] += 1
            except OSError:
                with lock:
                    out["done"] += 1
                    out["errors"] += 1
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(time_limit)
    with lock:
        out["hung"] = out["total"] - out["done"]
        snap = dict(out)
    close = getattr(reader, "close", None)
    if close and not worker.is_alive():
        try:
            close()
        except OSError:
            pass
    snap["rating"] = rate(snap["errors"], snap["slow"], snap["hung"], max(1, snap["total"]))
    snap["text"] = TEXT[snap["rating"]]
    return snap
