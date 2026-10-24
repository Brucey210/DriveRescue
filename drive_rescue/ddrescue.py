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


SLOW_AREA_RATE = 64 * 1024  # bytes per second; below this a stretch of the drive is skipped for now


def sector_args(sector):
    """ddrescue assumes 512-byte sectors. With direct disc access (-d) a drive that really uses
    4096-byte sectors refuses reads that are not a multiple of its own size, so say what it is."""
    return ["-b", str(int(sector))] if sector and int(sector) != 512 else []


def build_command(binary, source, image, mapfile, phase, opts, sector=512):
    """Return the argument list for one ddrescue pass.

    The source is only ever the *input* file; ddrescue never writes to it.
    """
    cmd = [binary, "-d", "-p"]  # -d direct disc access, -p preallocate the image
    cmd += sector_args(sector)
    cmd += STYLES.get(opts.get("style", "gentle"), STYLES["gentle"])
    if opts.get("reopen"):
        cmd += ["--reopen-on-error"]  # a USB drive that wedges after an error gets a fresh connection
    if phase in ("quick", "meta"):
        cmd += ["-n"]
        if phase == "meta" and opts.get("area"):
            pos, length = opts["area"]
            cmd += ["-i", str(int(pos)), "-s", str(int(length))]  # only this stretch of the drive
        if opts.get("min_read_rate") and phase == "quick":
            # Where the drive crawls, move on and come back later: the good parts are saved first
            # and a weak area can't eat hours. The retry pass covers whatever was skipped.
            cmd += [f"--min-read-rate={SLOW_AREA_RATE}"]  # no scraping: skip trouble spots for now
        if opts.get("no_trim"):
            cmd += ["--no-trim"]  # and don't slowly trim around them yet; the retry pass does that
    elif phase in ("retry", "reverse"):
        cmd += ["-r", str(int(opts.get("retries", 3)))]
        backward = opts.get("direction") == "backward"
        if phase == "reverse":
            backward = not backward  # the second look is from the other end
            if opts.get("retrim"):
                cmd += ["--retrim"]  # re-read the edges of bad areas that were given up on
        if backward:
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


_FEATURES = {}


def detect_features(system, binary):
    """What this ddrescue build understands, read from its own --help. Unknown means off."""
    if binary not in _FEATURES:
        try:
            _rc, out, err = system.run([binary, "--help"], timeout=10)
        except Exception:
            out = err = ""
        text = (out or "") + (err or "")
        _FEATURES[binary] = {"no_trim": "--no-trim" in text,
                             "min_read_rate": "--min-read-rate" in text,
                             "reopen": "--reopen-on-error" in text,
                             "retrim": "--retrim" in text}
    return _FEATURES[binary]


def explain_exit(code):
    """A plain sentence for a ddrescue exit status (see its manual: 1 environment, 2 input, 3 bug)."""
    return {1: "ddrescue could not use one of the drives or files, or was given a bad option.",
            2: "ddrescue found its progress file damaged or invalid.",
            3: "ddrescue hit an internal error."}.get(code, "")
