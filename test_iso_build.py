"""The ISO build script, run against stand-in tools (the real xorriso/squashfs are not needed here).

This checks the script's own logic: what it adds, the order of steps, and that it keeps the
boot setup. It cannot prove the finished ISO boots; that needs a real SystemRescue ISO.
"""
import os
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "iso", "build-iso.sh")

XORRISO = r'''#!/bin/sh
echo "xorriso $*" >> "$STUB_LOG"
case "$*" in
  *"-find / -name airootfs.sfs"*) echo "'/sysresccd/x86_64/airootfs.sfs'";;
  *"-find / -name airootfs.sha512"*) echo "'/sysresccd/x86_64/airootfs.sha512'";;
  *"-osirrox on"*) for a; do last=$a; done; echo squash > "$last";;
  *"-outdev"*) for a; do prev=$cur; cur=$a; [ "$prev" = "-outdev" ] && out=$a; done; echo iso > "$out";;
  *"-find /sysrescue.d"*) echo "'/sysrescue.d/500-driverescue.yaml'";;
  *"-toc"*) echo "Boot record  : El Torito";;
esac
'''
UNSQ = r'''#!/bin/sh
echo "unsquashfs $*" >> "$STUB_LOG"
if [ "$1" = "-s" ]; then printf 'Compression zstd\nBlock size 1048576\n'; exit 0; fi
while [ $# -gt 0 ]; do [ "$1" = "-d" ] && dir=$2; shift; done
mkdir -p "$dir/etc" "$dir/opt" "$dir/usr"
'''
MKSQ = r'''#!/bin/sh
echo "mksquashfs $*" >> "$STUB_LOG"
(cd "$1" && find . -type f) > "$STUB_LOG.tree"
echo new > "$2"
'''


@unittest.skipUnless(os.geteuid() == 0, "the script insists on running as root")
class BuildScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="isotest-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, body in (("xorriso", XORRISO), ("unsquashfs", UNSQ), ("mksquashfs", MKSQ)):
            path = os.path.join(self.bin, name)
            open(path, "w").write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.log = os.path.join(self.tmp, "log")
        self.base = os.path.join(self.tmp, "base.iso")
        open(self.base, "w").write("x")
        self.out = os.path.join(self.tmp, "out.iso")

    def run_script(self, *args):
        env = {**os.environ, "PATH": self.bin + ":" + os.environ["PATH"], "STUB_LOG": self.log}
        return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env, timeout=60)

    def test_builds_keeps_boot_setup_and_adds_settings(self):
        r = self.run_script(self.base, self.out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = open(self.log).read()
        self.assertIn("-boot_image any replay", log)                       # boot records carried across
        self.assertIn(f"-indev {self.base} -outdev {self.out}", log)       # the original is only read
        self.assertIn("/sysrescue.d/500-driverescue.yaml", log)           # desktop starts by itself
        self.assertIn("/sysresccd/x86_64/airootfs.sha512", log)
        self.assertIn("-comp zstd", log)                                   # same compression as the original
        self.assertIn("-b 1048576", log)
        tree = open(self.log + ".tree").read()
        for need in ("./opt/drive-rescue/start.sh", "./opt/drive-rescue/drive_rescue/server.py",
                     "./opt/drive-rescue/drive_rescue/ui/index.html", "./etc/xdg/autostart/drive-rescue.desktop"):
            self.assertIn(need, tree)
        self.assertNotIn("__pycache__", tree)
        self.assertEqual(open(self.base).read(), "x")
        self.assertTrue(os.path.exists(self.out + ".sha256"))

    def test_refuses_a_missing_or_wrong_input(self):
        self.assertNotEqual(self.run_script(os.path.join(self.tmp, "nope.iso")).returncode, 0)
        self.assertNotEqual(self.run_script().returncode, 0)

    def test_stops_when_it_is_not_a_systemrescue_iso(self):
        open(os.path.join(self.bin, "xorriso"), "w").write("#!/bin/sh\nexit 0\n")
        r = self.run_script(self.base, self.out)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SystemRescue", r.stderr)
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
