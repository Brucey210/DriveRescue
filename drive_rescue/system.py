"""The only place that talks to the operating system.

Everything else goes through a System object, so tests can swap in a fake one
that never touches a real disk.
"""
import json
import os
import shutil
import subprocess


class System:
    """Real system access (run as root on the rescue USB)."""

    def run(self, cmd, timeout=30, input_text=None):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            return 127, "", f"{cmd[0]}: not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"{cmd[0]}: timed out after {timeout}s"

    def which(self, name):
        return shutil.which(name)

    # ----- drives ---------------------------------------------------------
    def lsblk(self):
        cols = "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,RM,FSTYPE,LABEL,MOUNTPOINTS,PKNAME"
        rc, out, _ = self.run(["lsblk", "-J", "-b", "-o", cols], timeout=15)
        if rc != 0:
            rc, out, _ = self.run(["lsblk", "-J", "-b", "-o", cols.replace("MOUNTPOINTS", "MOUNTPOINT")], timeout=15)
        if rc != 0:
            return []
        try:
            return json.loads(out).get("blockdevices", [])
        except ValueError:
            return []

    def smartctl_json(self, path):
        for extra in ([], ["-d", "sat"]):
            rc, out, _ = self.run(["smartctl", "-j", "-H", "-A", *extra, path], timeout=20)
            try:
                data = json.loads(out)
            except ValueError:
                continue
            if data.get("smart_status") is not None or data.get("ata_smart_attributes"):
                return data
        return None

    def block_size(self, path):
        rc, out, _ = self.run(["blockdev", "--getsize64", path], timeout=10)
        if rc == 0 and out.strip().isdigit():
            return int(out.strip())
        try:
            return os.stat(path).st_size
        except OSError:
            return 0

    def set_readonly(self, path):
        """Ask the kernel to refuse every write to this drive."""
        rc, _out, err = self.run(["blockdev", "--setro", path], timeout=10)
        return rc == 0, err.strip()

    # ----- mounting -------------------------------------------------------
    def mount(self, device, mountpoint, fstype=None, read_only=False):
        os.makedirs(mountpoint, exist_ok=True)
        opts = "ro" if read_only else "rw"
        if read_only and fstype in ("ext2", "ext3", "ext4"):
            opts += ",noload"  # do not replay the journal on a damaged image
        attempts = []
        if fstype == "ntfs":
            attempts = [["mount", "-t", "ntfs3", "-o", opts, device, mountpoint],
                        ["mount", "-t", "ntfs-3g", "-o", opts, device, mountpoint]]
        elif fstype:
            attempts = [["mount", "-t", fstype, "-o", opts, device, mountpoint]]
        else:
            attempts = [["mount", "-o", opts, device, mountpoint]]
        last = ""
        for cmd in attempts:
            rc, _out, err = self.run(cmd, timeout=60)
            if rc == 0:
                return True, ""
            last = err.strip() or f"mount exited with code {rc}"
        return False, last

    def umount(self, mountpoint):
        rc, _out, err = self.run(["umount", mountpoint], timeout=60)
        return rc == 0, err.strip()

    def free_bytes(self, path):
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize

    # ----- loop devices (for opening the image) ---------------------------
    def attach_image(self, image):
        rc, out, err = self.run(["losetup", "--read-only", "--find", "--show", "--partscan", image], timeout=30)
        if rc != 0:
            return None, err.strip() or "could not attach the image"
        return out.strip(), ""

    def detach_image(self, loop):
        self.run(["losetup", "-d", loop], timeout=30)

    def power(self, action):
        """Shut down or restart the computer (only ever used when running from the boot disc)."""
        self.run(["sync"], timeout=120)
        cmd = ["systemctl", "poweroff"] if action == "poweroff" else ["systemctl", "reboot"]
        rc, _out, err = self.run(cmd, timeout=30)
        return rc == 0, err.strip()

    def sector_size(self, path):
        """Logical sector size in bytes (512 for plain files and when the drive won't say)."""
        rc, out, _ = self.run(["blockdev", "--getss", path], timeout=10)
        if rc == 0 and out.strip().isdigit() and int(out.strip()) in (512, 1024, 2048, 4096):
            return int(out.strip())
        return 512

    def attach_region(self, path, offset, size):
        """A writable loop device covering just one partition's bytes, so filesystem tools can be
        pointed at it without waiting for the kernel to re-read the partition table."""
        rc, out, err = self.run(["losetup", "--find", "--show", "--offset", str(offset),
                                 "--sizelimit", str(size), path], timeout=30)
        if rc != 0:
            return None, err.strip() or "could not attach the partition"
        return out.strip(), ""

    def probe(self, path):
        """Filesystem type and label read straight from the device (lsblk needs udev for these)."""
        rc, out, _err = self.run(["blkid", "-o", "export", path], timeout=15)
        info = {}
        if rc == 0:
            for line in out.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    info[key.strip()] = value.strip()
        return info
