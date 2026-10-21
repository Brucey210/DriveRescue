"""Reading GNU ddrescue mapfiles.

The mapfile is the single source of truth about which parts of the drive have
been copied. Its status characters are:

    ?  not tried yet            *  tried, needs trimming
    /  needs scraping           -  bad sector (unreadable so far)
    +  copied successfully
"""
from dataclasses import dataclass, field

SKIPPED = ("*", "/", "-")


@dataclass
class Mapfile:
    current_pos: int = 0
    current_status: str = "?"
    current_pass: int = 1
    segments: list = field(default_factory=list)  # (pos, size, status)


def parse_mapfile(text: str) -> Mapfile:
    mf = Mapfile()
    seen_status_line = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not seen_status_line:
            seen_status_line = True
            try:
                mf.current_pos = int(parts[0], 0)
                mf.current_status = parts[1] if len(parts) > 1 else "?"
                mf.current_pass = int(parts[2]) if len(parts) > 2 else 1
            except (ValueError, IndexError):
                pass
            continue
        if len(parts) < 3:
            continue
        try:
            pos, size = int(parts[0], 0), int(parts[1], 0)
        except ValueError:
            continue
        mf.segments.append((pos, size, parts[2]))
    return mf


def summarize(mf: Mapfile, total: int) -> dict:
    sums = {"finished": 0, "skipped": 0, "untried": 0}
    for _pos, size, st in mf.segments:
        if st == "+":
            sums["finished"] += size
        elif st in SKIPPED:
            sums["skipped"] += size
        else:
            sums["untried"] += size
    covered = sums["finished"] + sums["skipped"] + sums["untried"]
    if total > covered:
        sums["untried"] += total - covered
    return sums


def build_cells(mf: Mapfile, total: int, n: int) -> list:
    """Downsample the mapfile into n cells: 'done', 'skipped' or 'waiting'."""
    if total <= 0 or n <= 0:
        return ["waiting"] * max(n, 0)
    done = [0.0] * n
    skipped = [False] * n
    cell_len = total / n
    for pos, size, st in mf.segments:
        if size <= 0 or pos >= total:
            continue
        end = min(pos + size, total)
        first = min(int(pos / cell_len), n - 1)
        last = min(int((end - 1) / cell_len), n - 1)
        for i in range(first, last + 1):
            if st == "+":
                lo = max(pos, i * cell_len)
                hi = min(end, (i + 1) * cell_len)
                if hi > lo:
                    done[i] += hi - lo
            elif st in SKIPPED:
                skipped[i] = True
    cells = []
    for i in range(n):
        if skipped[i]:
            cells.append("skipped")
        elif done[i] >= cell_len * 0.999:
            cells.append("done")
        else:
            cells.append("waiting")
    return cells


def outlook(mf: Mapfile, total: int):
    """A plain-language estimate of how the rescue is likely to end, from what has been read so far.
    Judged on the area already tried, so it is meaningful once the quick pass has gone over the drive."""
    sums = summarize(mf, total)
    tried = sums["finished"] + sums["skipped"]
    if total <= 0 or tried <= 0:
        return None
    bad = [size for _pos, size, st in mf.segments if st in SKIPPED and size > 0]
    share = 100.0 * sums["skipped"] / tried
    info = {"bad_bytes": sums["skipped"], "bad_areas": len(bad), "largest_bad": max(bad) if bad else 0,
            "good_pct": round(100.0 * sums["finished"] / total, 2), "unread_bytes": sums["untried"]}
    if sums["skipped"] == 0:
        info.update(level="excellent", title="Excellent",
                    text="Nothing unreadable has turned up so far. Everything read cleanly.")
    elif share <= 0.01 or sums["skipped"] <= 1024 * 1024:
        info.update(level="excellent", title="Excellent",
                    text="Almost everything read cleanly. The gaps are tiny, so nearly all files should open normally.")
    elif share <= 0.5:
        info.update(level="good", title="Very good",
                    text="The great majority is safe. A few files may be damaged where the drive could not be read, "
                         "and the retry pass may win some of that back.")
    elif share <= 5:
        info.update(level="fair", title="Fair",
                    text="A noticeable part could not be read. Expect some damaged files. Retrying is worth the time, "
                         "and a file scan can find files whose folders were lost.")
    else:
        info.update(level="poor", title="Heavy damage",
                    text="A large part of the drive is not reading. Many files will be damaged. Let the retry pass run, "
                         "and if the files are very valuable, consider a professional recovery service before "
                         "stressing the drive further.")
    return info
