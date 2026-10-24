"""A small local web server. The guided screens talk to the engine through it.

It only listens on this computer (127.0.0.1), checks the Host header, and requires a random
token that is handed to the page when it loads, so other web pages can't drive it.
"""
import argparse
import atexit
import json
import os
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import clone as clone_mod
from . import drives as drives_mod
from . import preflight as preflight_mod
from . import report as report_mod
from . import plan as plan_mod
from .job import ImageJob, JobError
from . import images as images_mod
from .recover import RecoverError, Recovery
from .sessionlog import SessionLog
from .system import System
from .util import human_size

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
STYLES = ("gentle", "fast")
DIRECTIONS = ("forward", "backward")
BUSY = ("running", "pausing", "finishing")


DESKTOP_FLAG = os.environ.get("DRIVE_RESCUE_DESKTOP_FLAG", "/tmp/drive-rescue-desktop")


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


def clean_opts(raw):
    raw = raw if isinstance(raw, dict) else {}
    style = raw.get("style", "gentle")
    direction = raw.get("direction", "forward")
    try:
        retries = int(raw.get("retries", 3))
    except (TypeError, ValueError):
        retries = 3
    return {"style": style if style in STYLES else "gentle",
            "retries": max(0, min(10, retries)),
            "direction": direction if direction in DIRECTIONS else "forward",
            "pause_after_quick": raw.get("pause_after_quick") is True,
            "thorough": raw.get("thorough") is True}


def explain_network_error(err):
    """A plain sentence for why a shared folder could not be opened. The system's own words are
    technical, so the common cases are translated and anything else is passed on after a hint."""
    low = (err or "").lower()
    if any(w in low for w in ("unknown filesystem type", "wrong fs type", "helper program", "mount.cifs: not found",
                              "mount.nfs: not found", "no such device")):
        return "This system doesn't have the software needed for that kind of share, so it can't connect."
    if any(w in low for w in ("permission denied", "logon_failure", "access denied", "authentication", "(13)")):
        return "The computer refused the user name or password, or this account isn't allowed to use that folder."
    if any(w in low for w in ("could not resolve", "name or service not known", "no route to host", "host is down",
                              "timed out", "unreachable", "(113)", "(112)", "(110)")):
        return "That computer could not be reached. Check the name or address, that it is switched on, and that it is on the same network."
    if any(w in low for w in ("no such file", "bad unc", "does not exist", "(2)", "not found")):
        return "The computer was found, but not a shared folder with that name. Check the spelling."
    return "Could not connect. Check the name, the shared folder and the password." + (f" ({err.strip()[-160:]})" if err and err.strip() else "")


def plan_summary(result):
    return {"ok": result["ok"], "errors": result["errors"], "warnings": result["warnings"],
            "need_text": result.get("need_text", ""), "free_text": result.get("free_text", ""),
            "resume": bool(result.get("resume")), "folder": result.get("folder"),
            "image": result.get("image")}


class App:
    """All the actions the screens can take. Kept free of HTTP so it can be tested directly."""

    def __init__(self, system=None):
        self.system = system or System()
        self.token = secrets.token_urlsafe(16)
        self.log = SessionLog()
        self.log.log("Drive Rescue started")
        self.job = None
        self.plan = None
        self.recovery = None
        self.network = None       # a shared folder that is mounted for saving the copy: {"mountpoint", "label"}
        self.last_task = None     # the last file-recovery summary, kept for the report
        self._found = None        # copies found on a drive when browsing a saved copy
        self._found_mount = None  # a drive opened only to look for them
        self._lock = threading.RLock()
        self.routes = {
            ("GET", "system/info"): self.system_info, ("POST", "system/power"): self.system_power, ("POST", "system/desktop"): self.system_desktop,
            ("GET", "preflight"): lambda q, b: preflight_mod.run_checks(self.system.which),
            ("GET", "drives"): self.drives, ("GET", "health"): self.health,
            ("POST", "clone/plan"): self.clone_plan, ("POST", "clone/start"): self.clone_start,
            ("POST", "plan"): self.make_plan, ("POST", "start"): self.start,
            ("POST", "network/connect"): self.network_connect, ("POST", "network/disconnect"): self.network_disconnect,
            ("GET", "report"): self.report, ("GET", "log"): self.session_log, ("GET", "recover/preview"): self.recover_preview,
            ("GET", "status"): self.status, ("POST", "pause"): self.pause,
            ("POST", "resume"): self.resume, ("POST", "stop"): self.stop,
            ("POST", "recover/open"): self.recover_open, ("GET", "recover/list"): self.recover_list,
            ("POST", "recover/copy"): self.recover_copy, ("POST", "recover/carve"): self.recover_carve,
            ("GET", "recover/status"): self.recover_status, ("POST", "recover/cancel"): self.recover_cancel,
            ("POST", "recover/close"): self.recover_close, ("POST", "recover/unlock"): self.recover_unlock,
            ("GET", "image/find"): self.image_find, ("POST", "image/open"): self.image_open,
            ("GET", "recover/search"): self.recover_search, ("GET", "recover/thumb"): self.recover_thumb,
        }

    def dispatch(self, method, route, query, body):
        fn = self.routes.get((method, route))
        if fn is None:
            raise ApiError("Not found.", 404)
        try:
            return fn(query, body if isinstance(body, dict) else {})
        except (JobError, RecoverError) as err:
            raise ApiError(str(err))

    # ---- drives
    def drives(self, q, b):
        return {"drives": drives_mod.list_drives(self.system), "busy": bool(self.job and self.job.state in BUSY)}

    def health(self, q, b):
        path = (q.get("path") or [""])[0]
        if path not in {d["path"] for d in drives_mod.list_drives(self.system)}:
            raise ApiError("That drive was not found.")
        return drives_mod.check_health(self.system, path)

    def _network_for(self, dest):
        """A destination called "network" means the shared folder connected earlier."""
        if dest != "network":
            return None
        if not self.network:
            raise ApiError("Connect to the shared folder first.")
        return self.network

    def make_plan(self, q, b):
        result = plan_mod.make_plan(self.system, b.get("source", ""), b.get("dest", ""), b.get("folder"),
                                    clean_opts(b.get("opts")), prepare=False, network=self._network_for(b.get("dest")))
        return plan_summary(result)

    # ---- saving to a shared folder on the network
    NET_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
    NET_SHARE = re.compile(r"^[A-Za-z0-9 _.$-]{1,80}$")
    NET_EXPORT = re.compile(r"^/[A-Za-z0-9 _./-]{0,200}$")

    def network_connect(self, q, b):
        kind, address = b.get("kind"), str(b.get("address", "")).strip()
        share, user, password = str(b.get("share", "")).strip(), str(b.get("user", "")).strip(), str(b.get("password", ""))
        if kind not in ("smb", "nfs"):
            raise ApiError("Choose Windows share or NFS.")
        if not self.NET_HOST.match(address):
            raise ApiError("Enter the computer's name or address, such as 192.168.1.20 or nas.local.")
        if kind == "smb" and not self.NET_SHARE.match(share):
            raise ApiError("Enter the name of the shared folder, such as Backup.")
        if kind == "nfs" and not self.NET_EXPORT.match(share):
            raise ApiError("Enter the shared path, which starts with a slash, such as /volume1/backup.")
        if len(user) > 80 or len(password) > 200 or any(c in user + password for c in "\r\n\0"):
            raise ApiError("That user name or password can't be used.")
        with self._lock:
            if self.job and self.job.state in BUSY:
                raise ApiError("A copy is running. Wait for it to finish before changing where it is saved.", 409)
            self._drop_network()
            mp = os.path.join(plan_mod.MOUNT_ROOT, "network")
            ok, err = self.system.mount_network(kind, address, share, mp, user, password)
            if not ok:
                raise ApiError(explain_network_error(err))
            if not os.access(mp, os.W_OK):
                self.system.umount(mp)
                raise ApiError("Connected, but the shared folder is read-only, so a copy can't be saved there.")
            label = f"{address}{'/' + share if kind == 'smb' else ':' + share}"
            self.network = {"mountpoint": mp, "label": label}
            try:
                free = human_size(self.system.free_bytes(mp))
            except OSError:
                free = ""
            self.log.log(f"Connected to a network folder ({kind}) {label}")
            return {"ok": True, "label": label, "free_text": free}

    def _drop_network(self):
        if self.network:
            self.system.umount(self.network["mountpoint"])
            self.network = None

    def network_disconnect(self, q, b):
        with self._lock:
            if self.job and self.job.state in BUSY:
                raise ApiError("A copy is running.", 409)
            self._drop_network()
        return {"ok": True}

    # ---- the computer itself (only on the boot disc, which sets DRIVE_RESCUE_ALLOW_POWER=1)
    @staticmethod
    def power_allowed():
        return os.environ.get("DRIVE_RESCUE_ALLOW_POWER") == "1"

    def system_info(self, q, b):
        return {"power": self.power_allowed()}

    def system_power(self, q, b):
        if not self.power_allowed():
            raise ApiError("Shutting down from here isn't available on this computer.", 403)
        action = b.get("action")
        if action not in ("poweroff", "reboot"):
            raise ApiError("Unknown action.")
        with self._lock:
            if self.job and self.job.state in BUSY:
                raise ApiError("A copy is still running. Pause or stop it first.", 409)
            if self.recovery and self.recovery.task.get("state") == "running":
                raise ApiError("Files are still being recovered. Wait for that to finish or cancel it.", 409)
            self.shutdown()
        self.log.log(f"Computer: {action}")
        ok, err = self.system.power(action)
        if not ok:
            raise ApiError(f"The computer did not respond: {err}", 500)
        return {"ok": True}

    def system_desktop(self, q, b):
        """Close the full-screen window and show the Linux desktop. Drive Rescue keeps running in the
        background (a copy in progress carries on) and can be opened again from the desktop."""
        if not self.power_allowed():
            raise ApiError("Leaving to the desktop isn't available on this computer.", 403)
        try:
            with open(DESKTOP_FLAG, "w") as fh:
                fh.write("1")
        except OSError as err:
            raise ApiError(f"Could not leave the program: {err}", 500)
        self.system.close_kiosk()
        return {"ok": True}

    # ---- cloning a whole drive
    @staticmethod
    def _clone_summary(result):
        keep = ("ok", "errors", "warnings", "health", "layout", "dest_contents", "shrink")
        out = {k: result.get(k) for k in keep}
        for side in ("source", "dest_drive"):
            d = result.get(side)
            out[side] = d and {"name": d["name"], "size_text": d["size_text"], "path": d["path"]}
        return out

    def _warn_if_rescue_copy_is_on(self, result):
        """Cloning erases the target. If this session's rescued copy is saved there, say so."""
        prev, dest = self.plan, result.get("dest_drive")
        if not prev or prev.get("mode") == "clone" or not dest or not prev.get("image"):
            return
        if (prev.get("dest_drive") or {}).get("path") == dest["path"]:
            result["warnings"].append("The rescued copy of the failing drive is saved on this target drive. Cloning "
                                      "erases it. Copy out any files you still need first, or pick another target.")

    def clone_plan(self, q, b):
        result = clone_mod.make_clone_plan(self.system, b.get("source", ""), b.get("dest", ""))
        self._warn_if_rescue_copy_is_on(result)
        return self._clone_summary(result)

    def clone_start(self, q, b):
        with self._lock:
            if self.job and self.job.state in BUSY:
                raise ApiError("A copy is already running.", 409)
            result = clone_mod.make_clone_plan(self.system, b.get("source", ""), b.get("dest", ""))
            if not result["ok"]:
                raise ApiError(" ".join(result["errors"]) or "The clone can't start.", 400, **self._clone_summary(result))
            if str(b.get("confirm", "")).strip().upper() != clone_mod.CONFIRM_WORD:
                raise ApiError(f"Type {clone_mod.CONFIRM_WORD} to confirm that the target drive will be erased.", 400)
            if self.recovery:
                self.recovery.close()
                self.recovery = None
            job = clone_mod.CloneJob(self.system, result, clone_mod.clean_clone_opts(b.get("opts")), log=self.log.log)
            self.log.log(f"Clone confirmed: {result['source']['name']} -> {result['dest_drive']['name']}")
            job.start()
            self.job, self.plan = job, result
            return self._clone_summary(result)

    # ---- copying
    def start(self, q, b):
        with self._lock:
            if self.job and self.job.state in BUSY:
                raise ApiError("A copy is already running.", 409)
            opts = clean_opts(b.get("opts"))
            result = plan_mod.make_plan(self.system, b.get("source", ""), b.get("dest", ""), b.get("folder"),
                                        opts, prepare=True, network=self._network_for(b.get("dest")))
            if not result["ok"]:
                raise ApiError(" ".join(result["errors"]) or "The copy can't start.", 400, **plan_summary(result))
            if self.recovery:
                self.recovery.close()
                self.recovery = None
            job = ImageJob(self.system, result, opts, log=self.log.log)
            self.log.log(f"Rescue copy confirmed: {result['source']['name']} ({result['source']['size_text']}) "
                         f"-> {result['image']}; options {opts}")
            job.start()
            if result.get("target_dir"):
                self.log.add_path(os.path.join(result["target_dir"], "DriveRescue-session.log"))
            self.job, self.plan = job, result
            return plan_summary(result)

    def _need_job(self):
        if not self.job:
            raise ApiError("No copy has been started.", 409)
        return self.job

    def status(self, q, b):
        if not self.job:
            return {"state": "none"}
        snap = self.job.snapshot()
        snap["folder"] = self.plan["folder"]
        snap["target_dir"] = self.plan["target_dir"]
        return snap

    def pause(self, q, b):
        self._need_job().pause()
        return self.status(q, b)

    def resume(self, q, b):
        job = self._need_job()
        with self._lock:
            if self.recovery:  # the image is about to be written to again, so let go of it
                self.last_task = self.recovery.status()
                self.recovery.close()
                self.recovery = None
        job.resume()
        return self.status(q, b)

    def stop(self, q, b):
        self._need_job().stop()
        return self.status(q, b)

    # ---- getting files back
    def _recovery(self):
        if not self.recovery:
            raise ApiError("Open the copy first.", 409)
        return self.recovery

    def recover_open(self, q, b):
        with self._lock:
            browsing = bool(self.plan and self.plan.get("mode") == "image")
            if not self.plan or (not self.job and not browsing):
                raise ApiError("There is no copy to open yet.", 409)
            if self.plan.get("mode") == "clone":
                raise ApiError("A cloned drive has no image to open.", 409)
            if self.job and self.job.state in BUSY:
                raise ApiError("Pause or finish the copy before opening it.", 409)
            if not os.path.exists(self.plan["image"]):
                raise ApiError("The copy file was not found on the healthy drive.", 409)
            if not self.recovery:
                self.recovery = Recovery(self.system, self.plan["image"], self.plan["target_dir"], self.plan.get("mapfile"))
            info = self.recovery.open()
            self.log.log(f"Opened the copy {os.path.basename(self.plan['image'])}: "
                         f"{len(info['volumes'])} readable section(s)")
            info["browsing"] = browsing
            info["image_name"] = os.path.basename(self.plan["image"])
            info["out_dir"] = "" if browsing else os.path.join(self.plan["target_dir"], "recovered")
            return info

    def recover_list(self, q, b):
        return self._recovery().list_dir((q.get("volume") or [""])[0], (q.get("rel") or [""])[0])

    def recover_copy(self, q, b):
        items = b.get("items")
        if not isinstance(items, list) or not items:
            raise ApiError("Choose at least one file or folder.")
        clean = [{"volume": str(i.get("volume", "")), "rel": str(i.get("rel", ""))}
                 for i in items if isinstance(i, dict)]
        rec = self._recovery()
        dest = self._recover_dest(b.get("dest"))
        self.log.log(f"Recovering {len(clean)} item(s) to {dest or 'the default folder'}")
        rec.start_copy(clean, dest)
        return rec.status()

    def recover_carve(self, q, b):
        rec = self._recovery()
        dest = self._recover_dest(b.get("dest"))
        self.log.log(f"Scanning for files, saving to {dest or 'the default folder'}")
        rec.start_carve(dest)
        return rec.status()

    def _recover_dest(self, dest):
        """The folder recovered files are saved in. None means the default: next to the copy of this session.
        Otherwise a drive partition or the connected network folder, plus a folder name on it."""
        if not dest:
            if self.plan and self.plan.get("mode") == "image":
                raise ApiError("Choose where to save the recovered files.")
            return None
        if not isinstance(dest, dict):
            raise ApiError("Choose where to save the recovered files.")
        kind = dest.get("kind")
        folder = plan_mod.clean_folder(dest.get("folder") or "Recovered")
        if kind == "same":
            if not self.plan or self.plan.get("mode") == "image" or not self.plan.get("target_dir"):
                raise ApiError("Choose a drive to save the recovered files on.")
            return self.plan["target_dir"]
        if kind == "network":
            if not self.network:
                raise ApiError("Connect to the shared folder first.")
            return os.path.join(self.network["mountpoint"], folder)
        if kind != "partition":
            raise ApiError("Choose where to save the recovered files.")
        drives = drives_mod.list_drives(self.system)
        drive, part = plan_mod.find_partition(drives, str(dest.get("path", "")))
        if part is None:
            raise ApiError("That drive was not found. Rescan and pick it again.")
        if drive["is_boot"]:
            raise ApiError("The rescue USB stick can't be used to save files. Pick another drive.")
        src = (self.plan or {}).get("source") or {}
        if src.get("path") and src["path"] == drive["path"]:
            raise ApiError("Files can't be saved on the failing drive. Pick a different drive.")
        if not part["dest_ok"]:
            raise ApiError(f"This partition can't be used: {part['dest_reason']}.")
        found = getattr(self, "_found", None)
        if found and found.get("part") == part["path"] and getattr(self, "_found_mount", None):
            ok, err = self.system.remount_rw(self._found_mount)  # it was opened read-only to look around
            if not ok:
                raise ApiError(plan_mod.explain_mount_error(err, part["fstype"]))
            return os.path.join(self._found_mount, folder)
        mp, err = plan_mod.ensure_mounted_rw(self.system, part)
        if err:
            raise ApiError(err)
        return os.path.join(mp, folder)

    # ---- opening a copy saved earlier
    def _release_found(self):
        """Let go of a drive that was opened only to look for copies on it."""
        mp = getattr(self, "_found_mount", None)
        if mp:
            self.system.umount(mp)
        self._found_mount = None

    def image_find(self, q, b):
        """Copies (image files) saved in one place: a drive partition, or the connected network folder.
        The drive is opened read-only and stays open so a copy on it can be browsed."""
        where = (q.get("where") or [""])[0]
        with self._lock:
            if self.recovery:  # e.g. the page was reloaded while a copy was open
                if self.recovery.task.get("state") == "running":
                    raise ApiError("Files are still being recovered. Wait for that to finish first.", 409)
                self.recovery.close()
                self.recovery = None
            self._release_found()
            part_path = None
            if where == "network":
                if not self.network:
                    raise ApiError("Connect to the shared folder first.")
                root = self.network["mountpoint"]
            else:
                drives = drives_mod.list_drives(self.system)
                drive, part = plan_mod.find_partition(drives, where)
                if part is None:
                    raise ApiError("That drive was not found. Rescan and pick it again.")
                if drive["is_boot"]:
                    raise ApiError("That is the rescue USB stick. Choose the drive your copy was saved on.")
                if not part["fstype"]:
                    raise ApiError("This partition has no file system that can be read.")
                part_path = part["path"]
                mounted = [m for m in part["mountpoints"] if m]
                if mounted:
                    root = mounted[0]
                else:
                    root = os.path.join(plan_mod.MOUNT_ROOT, "find-" + part["name"])
                    ok, err = self.system.mount(part["path"], root, part["fstype"], read_only=True)
                    if not ok:
                        raise ApiError(plan_mod.explain_mount_error(err, part["fstype"]))
                    self._found_mount = root
            items = images_mod.scan(root)
            self._found = {"where": where, "part": part_path, "root": root,
                           "items": {os.path.relpath(i["path"], root): i for i in items}}
            return {"images": [{"id": rel, **{k: i[k] for k in ("name", "folder", "size_text", "mtime", "model", "percent")}}
                               for rel, i in self._found["items"].items()]}

    def image_open(self, q, b):
        """Open one of the copies image_find listed, so its files can be browsed and recovered."""
        found = getattr(self, "_found", None)
        rel = str(b.get("id", ""))
        if not found or rel not in found["items"]:
            raise ApiError("Look for the copy again.", 409)
        item = found["items"][rel]
        with self._lock:
            if self.job and self.job.state in BUSY:
                raise ApiError("A copy is running. Wait for it to finish first.", 409)
            if self.recovery:
                self.recovery.close()
                self.recovery = None
            if not os.path.exists(item["path"]):
                raise ApiError("That copy could not be found any more.", 409)
            self.job = None
            self.plan = {"mode": "image", "image": item["path"], "mapfile": item["mapfile"],
                         "target_dir": os.path.dirname(item["path"]), "folder": item["folder"], "source": {}}
            self.last_task = None
            return {"ok": True, "name": item["name"]}

    def recover_unlock(self, q, b):
        password = b.get("password")
        if not isinstance(password, str) or len(password) > 500 or "\0" in password:
            raise ApiError("That password can't be used.")
        info = self._recovery().unlock(str(b.get("id", "")), password)
        self.log.log("Opened a protected section of the copy")  # never the password
        return info

    def recover_search(self, q, b):
        return self._recovery().search((q.get("volume") or [""])[0], (q.get("q") or [""])[0], (q.get("kind") or [""])[0])

    def recover_thumb(self, q, b):
        return self._recovery().thumbnail((q.get("volume") or [""])[0], (q.get("rel") or [""])[0])

    def recover_status(self, q, b):
        return self._recovery().status()

    def recover_cancel(self, q, b):
        self._recovery().cancel()
        return {"ok": True}

    def recover_preview(self, q, b):
        return self._recovery().preview((q.get("volume") or [""])[0], (q.get("rel") or [""])[0])

    def recover_close(self, q, b):
        with self._lock:
            if self.recovery:
                self.last_task = self.recovery.status()
                self.recovery.close()
                self.recovery = None
            self._release_found()
        return {"ok": True}

    def report(self, q, b):
        if self.plan and self.plan.get("mode") == "image":
            task = self.recovery.status() if self.recovery else self.last_task
            text = self._with_log(report_mod.build_image_report(self.plan, task))
            path = os.path.splitext(self.plan["image"])[0] + ".recovery-report.txt"
            saved = ""
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                saved = path
            except OSError:
                pass  # the copy may be on a read-only drive; the report is still downloaded
            return {"text": text, "filename": "DriveRescue-report.txt", "saved_to": saved}
        if not self.job or not self.plan:
            raise ApiError("There is nothing to report on yet.", 409)
        snap = self.status(q, b)
        health = None
        try:
            health = drives_mod.check_health(self.system, self.plan["source"]["path"])
        except Exception:
            pass
        task = self.recovery.status() if self.recovery else self.last_task
        text = self._with_log(report_mod.build_report(self.plan, snap, health, task))
        saved = ""
        if self.plan.get("mode") != "clone" and self.plan.get("image"):
            path = os.path.splitext(self.plan["image"])[0] + ".report.txt"
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                saved = path
            except OSError:
                pass
        return {"text": text, "filename": "DriveRescue-report.txt", "saved_to": saved}

    def _with_log(self, text):
        log = self.log.text()
        return text + ("\nSESSION LOG\n" + log if log else "")

    def session_log(self, q, b):
        return {"text": self.log.text()}

    def shutdown(self):
        with self._lock:
            if self.job and self.job.state in BUSY:
                self.job.pause()
            if self.recovery:
                self.recovery.close()
            self._release_found()
            self._drop_network()


# ------------------------------------------------------------------------- HTTP


def make_handler(app, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DriveRescue"

        def log_message(self, *args):
            pass

        def _send(self, status, payload, ctype="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return (self.headers.get("Host") or "") in allowed_hosts

        def _route(self, method):
            if not self._host_ok():
                return self._send(403, {"error": "Not allowed."})
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                with open(os.path.join(UI_DIR, "index.html"), "rb") as fh:
                    page = fh.read().replace(b"__TOKEN__", app.token.encode())
                return self._send(200, page, "text/html; charset=utf-8")
            if not url.path.startswith("/api/"):
                return self._send(404, {"error": "Not found."})
            if not secrets.compare_digest(self.headers.get("X-Token") or "", app.token):
                return self._send(403, {"error": "Not allowed."})
            body = {}
            if method == "POST":
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, TypeError):
                    return self._send(400, {"error": "That request could not be read."})
            try:
                return self._send(200, app.dispatch(method, url.path[5:].strip("/"), parse_qs(url.query), body))
            except ApiError as err:
                return self._send(err.status, {"error": str(err), **err.extra})
            except Exception as err:  # keep the screens alive, and say something useful
                return self._send(500, {"error": f"Unexpected problem: {err}"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def serve(app, host="127.0.0.1", port=8765):
    allowed = set()
    httpd = ThreadingHTTPServer((host, port), make_handler(app, allowed))
    httpd.daemon_threads = True
    port = httpd.server_address[1]  # the real port when 0 was asked for
    allowed.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    return httpd, f"http://127.0.0.1:{port}/"


def main():
    parser = argparse.ArgumentParser(description="Drive Rescue")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    app = App()
    atexit.register(app.shutdown)
    httpd, url = serve(app, port=args.port)
    print(f"Drive Rescue is running at {url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
