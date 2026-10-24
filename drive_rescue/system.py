"""The only place that talks to the operating system.

Everything else goes through a System object, so tests can swap in a fake one
that never touches a real disk.
"""
import json
import os
import shutil
import subprocess


# A drive that failed was usually not shut down cleanly, so its journal or log is dirty. These are the
# read-only ways to open such a filesystem, tried in order. Every one of them is read-only: none can
# replay a journal or write anything back to the copy.
RO_OPTIONS = {
    "ext2": ["ro,noload"], "ext3": ["ro,noload"], "ext4": ["ro,noload"],
    "xfs": ["ro,norecovery,nouuid"],
    "btrfs": ["ro", "ro,rescue=usebackuproot", "ro,usebackuproot", "ro,rescue=all"],
    "hfsplus": ["ro", "ro,force"],
}


def mount_attempts(device, mountpoint, fstype=None, read_only=False):
    """The mount commands to try, most careful first."""
    if not fstype:
        return [["mount", "-o", "ro" if read_only else "rw", device, mountpoint]]
    if fstype == "ntfs":
        if read_only:  # ntfs3 first; ntfs-3g can open volumes Windows left hibernated or unclean
            variants = [("ntfs3", "ro"), ("ntfs-3g", "ro,norecover"), ("ntfs-3g", "ro")]
        else:
            variants = [("ntfs3", "rw"), ("ntfs-3g", "rw")]
        return [["mount", "-t", t, "-o", o, device, mountpoint] for t, o in variants]
    options = RO_OPTIONS.get(fstype, ["ro"]) if read_only else ["rw"]
    return [["mount", "-t", fstype, "-o", o, device, mountpoint] for o in options]


def network_mount_attempts(kind, address, share, mountpoint, credfile=None):
    """Mount commands for a shared folder on another computer, most modern first."""
    if kind == "smb":
        auth = f"credentials={credfile}" if credfile else "guest"
        return [["mount", "-t", "cifs", f"//{address}/{share}", mountpoint, "-o", f"{auth},vers={v},iocharset=utf8"]
                for v in ("3.0", "2.1")] + [["mount", "-t", "cifs", f"//{address}/{share}", mountpoint, "-o", f"{auth},iocharset=utf8"]]
    if kind == "nfs":
        return [["mount", "-t", "nfs", "-o", o, f"{address}:{share}", mountpoint] for o in ("vers=4,rw", "vers=3,rw,nolock")]
    raise ValueError(f"unknown kind {kind!r}")


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

    def set_readwrite(self, path):
        """Undo set_readonly. Only the clone target is ever made writable again."""
        rc, _out, err = self.run(["blockdev", "--setrw", path], timeout=10)
        return rc == 0, err.strip()

    # ----- mounting -------------------------------------------------------
    def mount(self, device, mountpoint, fstype=None, read_only=False):
        os.makedirs(mountpoint, exist_ok=True)
        attempts = mount_attempts(device, mountpoint, fstype, read_only)
        last = ""
        for cmd in attempts:
            rc, _out, err = self.run(cmd, timeout=60)
            if rc == 0:
                return True, ""
            last = err.strip() or f"mount exited with code {rc}"
        return False, last

    def mount_network(self, kind, address, share, mountpoint, user="", password=""):
        """Mount a shared folder. The password goes in a private file that exists only for the mount,
        never on the command line where other programs could read it."""
        os.makedirs(mountpoint, exist_ok=True)
        credfile = None
        try:
            if kind == "smb" and user:
                import tempfile
                fd, credfile = tempfile.mkstemp(prefix="dr-cred-", dir="/run" if os.path.isdir("/run") else None)
                with os.fdopen(fd, "w") as fh:
                    fh.write(f"username={user}\npassword={password}\n")
            last = ""
            for cmd in network_mount_attempts(kind, address, share, mountpoint, credfile):
                rc, _out, err = self.run(cmd, timeout=45)
                if rc == 0:
                    return True, ""
                last = err.strip() or f"mount exited with code {rc}"
            return False, last
        finally:
            if credfile:
                try:
                    os.remove(credfile)
                except OSError:
                    pass

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

    def partition_offset(self, path):
        """Where a partition starts inside its whole disk, in bytes (0 for a whole disk or when unknown)."""
        name = os.path.basename(os.path.realpath(path))
        try:
            with open(f"/sys/class/block/{name}/start") as fh:
                return int(fh.read().strip()) * 512
        except (OSError, ValueError):
            return 0

    def dm_create(self, name, table):
        """A read-only virtual disk built from a table. Used so the unrecovered parts of the copy
        answer with a read error instead of silently reading as blanks."""
        rc, _out, err = self.run(["dmsetup", "create", name, "--readonly"], timeout=30, input_text=table)
        if rc != 0:
            return None, err.strip() or "dmsetup failed"
        self.run(["dmsetup", "mknodes", name], timeout=10)
        path = "/dev/mapper/" + name
        return (path, "") if os.path.exists(path) else (None, "the virtual disk did not appear")

    def dm_remove(self, name):
        self.run(["dmsetup", "remove", "--retry", name], timeout=30)

    def detach_image(self, loop):
        self.run(["losetup", "-d", loop], timeout=30)

    def power(self, action):
        """Shut down or restart the computer (only ever used when running from the boot disc)."""
        self.run(["sync"], timeout=120)
        cmd = ["systemctl", "poweroff"] if action == "poweroff" else ["systemctl", "reboot"]
        rc, _out, err = self.run(cmd, timeout=30)
        return rc == 0, err.strip()

    def close_kiosk(self):
        """Close the full-screen window (the one started with the kiosk profile)."""
        self.run(["pkill", "-f", "drive-rescue-" + "profile"], timeout=10)

    def firmware(self):
        """How this computer started: "uefi" or "bios"."""
        return "uefi" if os.path.isdir("/sys/firmware/efi") else "bios"

    def used_bytes(self, part, scratch):
        """Space used by the files on a partition, or None when it can't be told. Reads only: a partition
        that is not mounted is opened read-only for a moment."""
        mounted = [m for m in part.get("mountpoints") or [] if m]
        mp, opened = (mounted[0], False) if mounted else (scratch, False)
        if not mounted:
            ok, _msg = self.mount(part["path"], mp, part.get("fstype") or None, read_only=True)
            if not ok:
                return None
            opened = True
        try:
            st = os.statvfs(mp)
            return (st.f_blocks - st.f_bfree) * st.f_frsize
        except OSError:
            return None
        finally:
            if opened:
                self.umount(mp)

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
