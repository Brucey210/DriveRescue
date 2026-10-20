"""Building ddrescue command lines and reading its live status output.

Copy works in two passes:
  quick  - grab everything that reads easily, skip trouble spots fast
  retry  - go back to the skipped areas, slowly, a few times
"""
import re
from .util import strip_ansi, parse_size

# Options chosen here are long-standing ddrescue options (1.19 and later).
STYLES = {
    # Gentle: smaller read blocks, so a bad spot costs less of the drive.
    "gentle": ["-c", "32"],
    "fast": [],
}


def build_command(binary, source, image, mapfile, phase, opts):
    """Return the argument list for one ddrescue pass.

    The source is only ever the *input* file; ddrescue never writes to it.
    """
    cmd = [binary, "-d", "-p"]  # -d direct disc access, -p preallocate the image
    cmd += STYLES.get(opts.get("style", "gentle"), STYLES["gentle"])
    if phase == "quick":
        cmd += ["-n"]  # no scraping: skip trouble spots for now
    elif phase == "retry":
        cmd += ["-r", str(int(opts.get("retries", 3)))]
        if opts.get("direction") == "backward":
            cmd += ["-R"]
    else:
        raise ValueError(f"unknown phase {phase!r}")
    cmd += [source, image, mapfile]
    return cmd


# "pct rescued: 38.99%" is a percentage, not a size, so it must not match.
_RESCUED = re.compile(r"(?<!pct )rescued:\s*([\d.,]+\s*[A-Za-z]+)")
_RATE = re.compile(r"current rate:\s*([\d.,]+\s*[A-Za-z]*)/s")
_ERRSIZE = re.compile(r"errsize:\s*([\d.,]+\s*[A-Za-z]*)")
_ERRORS = re.compile(r"read errors:\s*(\d+)")


def parse_status(text: str) -> dict:
    """Pull the latest numbers out of ddrescue's status screen text."""
    clean = strip_ansi(text).replace("\r", "\n")
    out = {}
    for key, rx in (("rescued", _RESCUED), ("rate", _RATE), ("errsize", _ERRSIZE)):
        found = rx.findall(clean)
        if found:
            value = parse_size(found[-1])
            if value is not None:
                out[key] = value
    found = _ERRORS.findall(clean)
    if found:
        out["errors"] = int(found[-1])
    return out
