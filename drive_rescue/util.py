"""Small helpers shared by the engine."""
import re

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def human_size(n: float) -> str:
    """Decimal sizes, the way drive makers label them (2 TB, 512 GB)."""
    n = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if n < 1000 or unit == "PB":
            if unit == "B":
                return f"{int(n)} B"
            text = f"{n:.1f}".rstrip("0").rstrip(".") if n < 100 else f"{n:.0f}"
            return f"{text} {unit}"
        n /= 1000.0
    return f"{n:.0f} PB"


def human_duration(seconds) -> str:
    if seconds is None or seconds < 0:
        return "calculating"
    seconds = int(seconds)
    if seconds < 60:
        return "under a minute"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} m"
    return f"{minutes} m"


def slug(text: str, default: str = "drive") -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return text[:40] or default


_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "pb": 1000**5,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "pib": 1024**5,
}


def parse_size(text: str):
    """'560 GB' -> bytes. Returns None when it cannot be understood."""
    m = re.match(r"^\s*([\d.,]+)\s*([A-Za-z]*)\s*$", text or "")
    if not m:
        return None
    number = m.group(1).replace(",", "")
    unit = (m.group(2) or "B").lower()
    # ddrescue writes "kB" for 1000 bytes and sometimes "k" or "M".
    if unit in ("k", "m", "g", "t", "p"):
        unit += "b"
    if unit not in _UNITS:
        return None
    try:
        return int(float(number) * _UNITS[unit])
    except ValueError:
        return None
