#!/usr/bin/env python3
"""Test stand-in for GNU ddrescue. NEVER used on a real drive.

Understands the options drive_rescue passes (-d -p -c N -n | -r N [-R]) and copies one regular
file to another, keeping a mapfile in ddrescue's real format, so a whole job can be tested
without a disk. Simulated damage, set by sidecar files next to the source:
  SRC.bad    "start length" byte ranges that never read
  SRC.flaky  ranges that fail the quick pass (-n) but read fine on a retry pass (-r)
Like ddrescue, it saves its mapfile and exits when interrupted (SIGINT).
Set FAKE_DELAY (seconds per block) to slow it down, FAKE_FAIL=1 to make it crash.
"""
import os
import signal
import sys
import time

BLOCK = 64 * 1024
interrupted = False


def on_sigint(signum, frame):
    global interrupted
    interrupted = True


def read_ranges(path):
    out = []
    if os.path.exists(path):
        for line in open(path):
            parts = line.split()
            if len(parts) == 2:
                out.append((int(parts[0]), int(parts[1])))
    return out


def hits(ranges, block):
    lo, hi = block * BLOCK, (block + 1) * BLOCK
    return any(s < hi and s + n > lo for s, n in ranges)


def load_states(mapfile, nblocks):
    states = ["?"] * nblocks
    if not os.path.exists(mapfile):
        return states
    first = True
    for line in open(mapfile):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if first:
            first = False
            continue
        pos, size, st = line.split()
        lo = int(pos, 0) // BLOCK
        hi = (int(pos, 0) + int(size, 0) + BLOCK - 1) // BLOCK
        for b in range(lo, min(hi, nblocks)):
            states[b] = st
    return states


def save_states(mapfile, states, current, pass_no):
    lines = [
        "# Mapfile. Created by fake ddrescue (tests only)",
        "# current_pos  current_status  current_pass",
        "0x%08X     ?               %d" % (current * BLOCK, pass_no),
        "#      pos        size  status",
    ]
    start = 0
    for i in range(1, len(states) + 1):
        if i == len(states) or states[i] != states[start]:
            lines.append("0x%08X  0x%08X  %s" % (start * BLOCK, (i - start) * BLOCK, states[start]))
            start = i
    tmp = mapfile + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, mapfile)


def fmt(n):
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return "%d %s" % (n, unit) if unit == "B" else "%.0f %s" % (n, unit)
        n /= 1000.0


def main():
    args, positional, flags = sys.argv[1:], [], set()
    i = 0
    while i < len(args):
        if args[i] in ("-c", "-r", "-T", "-b"):
            flags.add(args[i])
            i += 2
        elif args[i].startswith("-"):
            flags.add(args[i])
            i += 1
        else:
            positional.append(args[i])
            i += 1
    src, dst, mapfile = positional
    signal.signal(signal.SIGINT, on_sigint)
    delay = float(os.environ.get("FAKE_DELAY", "0.01"))
    if os.environ.get("FAKE_FAIL"):
        sys.stdout.write("ddrescue: Input/output error\n")
        sys.exit(1)

    size = os.path.getsize(src)
    nblocks = (size + BLOCK - 1) // BLOCK
    bad, flaky = read_ranges(src + ".bad"), read_ranges(src + ".flaky")
    states = load_states(mapfile, nblocks)
    retry = "-r" in flags
    pass_no = 2 if retry else 1
    wanted = "?*/-" if retry else "?"
    order = range(nblocks - 1, -1, -1) if "-R" in flags else range(nblocks)

    if "-p" in flags and (not os.path.exists(dst) or os.path.getsize(dst) < size):
        with open(dst, "ab"):
            pass
        os.truncate(dst, size)  # preallocate, like ddrescue -p
    last_save, start = time.time(), time.time()
    with open(src, "rb") as fin, open(dst, "r+b" if os.path.exists(dst) else "w+b") as fout:
        for b in order:
            if interrupted:
                break
            if states[b] not in wanted:
                continue
            time.sleep(delay)
            if hits(bad, b):
                states[b] = "-" if retry else "*"
            elif hits(flaky, b) and not retry:
                states[b] = "*"
            else:
                fin.seek(b * BLOCK)
                fout.seek(b * BLOCK)
                fout.write(fin.read(BLOCK))
                states[b] = "+"
            rescued = sum(BLOCK for s in states if s == "+")
            elapsed = max(time.time() - start, 0.001)
            sys.stdout.write(
                "\x1b[1A\r     ipos: %9s, non-trimmed:        0 B,  current rate:   %d MB/s\n"
                "  rescued: %9s,   bad areas:        0,        run time:      %ds\n"
                "pct rescued: %6.2f%%, read errors:        0,  remaining time:      3m 11s\n"
                % (fmt(b * BLOCK), 92, fmt(rescued), elapsed, rescued * 100.0 / size)
            )
            sys.stdout.flush()
            if time.time() - last_save > 0.1:
                save_states(mapfile, states, b, pass_no)
                last_save = time.time()
    save_states(mapfile, states, 0, pass_no)
    if interrupted:
        sys.stdout.write("\nddrescue: Interrupted by user\n")
        sys.exit(130)


if __name__ == "__main__":
    main()
