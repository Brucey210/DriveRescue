#!/usr/bin/env python3
"""Stand-in for PhotoRec: writes two 'found' files into the /d directory and exits."""
import os
import sys

args = sys.argv[1:]
outdir = args[args.index("/d") + 1]
os.makedirs(os.path.join(outdir, "recup_dir.1"), exist_ok=True)
for name in ("f0001.jpg", "f0002.doc"):
    with open(os.path.join(outdir, "recup_dir.1", name), "wb") as fh:
        fh.write(b"carved " + name.encode())
