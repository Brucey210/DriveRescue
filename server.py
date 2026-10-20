"""A small local web server. The guided screens talk to the engine through it.

It only listens on this computer (127.0.0.1), checks the Host header, and requires a random
token that is handed to the page when it loads, so other web pages can't drive it.
"""
import argparse
import atexit
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import clone as clone_mod
from . import drives as drives_mod
from . import preflight as preflight_mod
from . import plan as plan_mod
from .job import ImageJob, JobError
from .recover import RecoverError, Recovery
from .system import System

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
STYLES = ("gentle", "fast")
DIRECTIONS = ("forward", "backward")
BUSY = ("running", "pausing", "finishing")


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
            "direction": direction if direction in DIRECTIONS else "forward"}


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
        self.job = None
        self.plan = None
        self.recovery = None
        self._lock = threading.RLock()
        self.routes = {
            ("GET", "system/info"): self.system_info, ("POST", "system/power"): self.system_power,
            ("GET", "preflight"): lambda q, b: preflight_mod.run_checks(self.system.which),
            ("GET", "drives"): self.drives, ("GET", "health"): self.health,
            ("POST", "clone/plan"): self.clone_plan, ("POST", "clone/start"): self.clone_start,
            ("POST", "plan"): self.make_plan, ("POST", "start"): self.start,
            ("GET", "status"): self.status, ("POST", "pause"): self.pause,
            ("POST", "resume"): self.resume, ("POST", "stop"): self.stop,
            ("POST", "recover/open"): self.recover_open, ("GET", "recover/list"): self.recover_list,
            ("POST", "recover/copy"): self.recover_copy, ("POST", "recover/carve"): self.recover_carve,
            ("GET", "recover/status"): self.recover_status, ("POST", "recover/cancel"): self.recover_cancel,
            ("POST", "recover/close"): self.recover_close,
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

    def make_plan(self, q, b):
        result = plan_mod.make_plan(self.system, b.get("source", ""), b.get("dest", ""), b.get("folder"),
                                    clean_opts(b.get("opts")), prepare=False)
        return plan_summary(result)

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
        ok, err = self.system.power(action)
        if not ok:
            raise ApiError(f"The computer did not respond: {err}", 500)
        return {"ok": True}

    # ---- cloning a whole drive
    @staticmethod
    def _clone_summary(result):
        keep = ("ok", "errors", "warnings", "health", "layout", "dest_contents")
        out = {k: result.get(k) for k in keep}
        for side in ("source", "dest_drive"):
            d = result.get(side)
            out[side] = d and {"name": d["name"], "size_text": d["size_text"], "path": d["path"]}
        return out

    def clone_plan(self, q, b):
        return self._clone_summary(clone_mod.make_clone_plan(self.system, b.get("source", ""), b.get("dest", "")))

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
            job = clone_mod.CloneJob(self.system, result, clone_mod.clean_clone_opts(b.get("opts")))
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
                                        opts, prepare=True)
            if not result["ok"]:
                raise ApiError(" ".join(result["errors"]) or "The copy can't start.", 400, **plan_summary(result))
            if self.recovery:
                self.recovery.close()
                self.recovery = None
            job = ImageJob(self.system, result, opts)
            job.start()
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
        self._need_job().resume()
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
            if not self.job or not self.plan:
                raise ApiError("There is no copy to open yet.", 409)
            if self.plan.get("mode") == "clone":
                raise ApiError("A cloned drive has no image to open.", 409)
            if self.job.state in BUSY:
                raise ApiError("Pause or finish the copy before opening it.", 409)
            if not os.path.exists(self.plan["image"]):
                raise ApiError("The copy file was not found on the healthy drive.", 409)
            if not self.recovery:
                self.recovery = Recovery(self.system, self.plan["image"], self.plan["target_dir"])
            info = self.recovery.open()
            info["out_dir"] = os.path.join(self.plan["target_dir"], "recovered")
            return info

    def recover_list(self, q, b):
        return self._recovery().list_dir((q.get("volume") or [""])[0], (q.get("rel") or [""])[0])

    def recover_copy(self, q, b):
        items = b.get("items")
        if not isinstance(items, list) or not items:
            raise ApiError("Choose at least one file or folder.")
        clean = [{"volume": str(i.get("volume", "")), "rel": str(i.get("rel", ""))}
                 for i in items if isinstance(i, dict)]
        self._recovery().start_copy(clean)
        return self._recovery().status()

    def recover_carve(self, q, b):
        self._recovery().start_carve()
        return self._recovery().status()

    def recover_status(self, q, b):
        return self._recovery().status()

    def recover_cancel(self, q, b):
        self._recovery().cancel()
        return {"ok": True}

    def recover_close(self, q, b):
        with self._lock:
            if self.recovery:
                self.recovery.close()
                self.recovery = None
        return {"ok": True}

    def shutdown(self):
        with self._lock:
            if self.job and self.job.state in BUSY:
                self.job.pause()
            if self.recovery:
                self.recovery.close()


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
