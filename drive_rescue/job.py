"""Running the copy: ddrescue in passes, with pause, resume and stop."""
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque

from . import ddrescue, mapfile
from .util import human_duration, human_size, strip_ansi

CELLS = 294
PHASES = [
    ("quick", "Quick copy", "Grabbing everything that reads easily"),
    ("retry", "Gentle retry", "Going back for skipped areas, slowly"),
    ("final", "Final check", "Making sure the copy is complete"),
]


class JobError(Exception):
    pass


class ImageJob:
    def __init__(self, system, plan, opts, binary=None, cells=CELLS, clock=time.time):
        self.system = system
        self.plan = plan
        self.opts = {"style": "gentle", "retries": 3, "direction": "forward", **(opts or {})}
        self.binary = binary or system.which("ddrescue") or system.which("gddrescue")
        self.cells = cells
        self.clock = clock
        self.size = plan["source"]["size"]
        self.state = "idle"  # idle running pausing paused done stopped error
        self.phase_index = 0
        self.message = ""
        self._proc = None
        self._thread = None
        self._lock = threading.Lock()
        self._want = None  # "pause" or "stop"
        self._tail = ""
        self._live = {}
        self._samples = deque(maxlen=20)
        self._speed = 0.0
        self._map_cache = (0.0, None)
        self.features = {}
        self._t0 = None      # when the current run started
        self._elapsed = 0.0  # time spent running in earlier runs
        self._peak = 0.0     # best speed seen, to tell when the drive is slowing down

    # ---- control ---------------------------------------------------------
    def start(self):
        if not self.binary:
            raise JobError("ddrescue is missing from this system, so copying can't start.")
        if not self.features:
            self.features = ddrescue.detect_features(self.system, self.binary)
        with self._lock:
            if self.state in ("running", "pausing"):
                return
            if self.state in ("idle",):
                self._prepare()
            self._begin_thread()

    def resume(self):
        with self._lock:
            if self.state in ("paused", "error", "stopped"):
                self._begin_thread()

    def pause(self):
        with self._lock:
            if self.state == "running":
                self._want = "pause"
                self.state = "pausing"
                self._signal()

    def stop(self):
        with self._lock:
            if self.state in ("running", "pausing"):
                self._want = "stop"
                self.state = "pausing"
                self._signal()
            elif self.state in ("paused", "error"):
                self.state = "stopped"

    def _signal(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGINT)  # ddrescue saves its progress, then exits
            except ProcessLookupError:
                pass

    # ---- set-up ----------------------------------------------------------
    def _prepare(self):
        src = self.plan["source"]
        ok, err = self.system.set_readonly(src["path"])
        if not ok:
            self.message = f"Could not lock the failing drive as read-only ({err}); copying reads only, never writes."
        for part in src["partitions"]:
            for mp in part["mountpoints"]:
                done, err = self.system.umount(mp)
                if not done:
                    raise JobError(f"Could not release {mp} on the failing drive: {err}")
        os.makedirs(self.plan["target_dir"], exist_ok=True)
        self._write_jobfile("created")

    def _write_jobfile(self, state):
        src = self.plan["source"]
        data = {"version": 1, "state": state, "image": self.plan["image"], "opts": self.opts,
                "source": {"path": src["path"], "model": src["model"], "serial": src["serial"], "size": src["size"]},
                "updated": int(self.clock())}
        try:
            with open(self.plan["jobfile"], "w") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass

    def _begin_thread(self):
        self._want = None
        self._t0 = self.clock()
        self.state = "running"
        self.message = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- the worker ------------------------------------------------------
    def _phase_keys(self):
        keys = ["quick"]
        if int(self.opts.get("retries", 3)) > 0:
            keys.append("retry")
        return keys

    def _run(self):
        try:
            keys = self._phase_keys()
            while self.phase_index < len(keys):
                key = keys[self.phase_index]
                if key == "retry" and self._summary()["skipped"] == 0:
                    self.phase_index += 1
                    continue
                rc = self._run_pass(key)
                if self._want == "pause":
                    self._finish("paused", "Paused. Your progress is saved.")
                    return
                if self._want == "stop":
                    self._finish("stopped", "Stopped. What was copied so far is kept.")
                    return
                if self._want == "lost":
                    self._finish("error", "The failing drive disappeared. It may have been unplugged, or it stopped "
                                          "responding. Check the cable and power, then start the copy again from the "
                                          "first screen. It carries on from where it stopped; nothing is lost.")
                    return
                if rc != 0:
                    tail = strip_ansi(self._tail).strip().splitlines()[-1:] or [""]
                    self._finish("error", f"The copy stopped unexpectedly (code {rc}). The drive may have stopped "
                                          f"responding. Your progress is saved, so you can resume. "
                                          f"{ddrescue.explain_exit(rc)} {tail[0][:160]}".replace("  ", " ").strip())
                    return
                self.phase_index += 1
                if (key == "quick" and self.opts.get("pause_after_quick") and self.phase_index < len(keys)
                        and self._summary()["skipped"] > 0):
                    self._finish("paused", "The quick copy is finished. Everything that reads easily is now safe in the "
                                           "image, so you can get your important files out now. Press Resume to go "
                                           "back for the difficult parts.")
                    return
            self._complete()
        except Exception as exc:  # never let the thread die silently
            self._finish("error", f"Unexpected problem: {exc}")

    def _command(self, key):
        return ddrescue.build_command(self.binary, self.plan["source"]["path"], self.plan["image"],
                                      self.plan["mapfile"], key, {**self.opts, **self.features},
                                      sector=self.plan.get("sector", 512))

    def _complete(self):
        self._finish("done", "")

    def _run_pass(self, key):
        cmd = self._command(key)
        self._tail = ""
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        if self._want:  # Pause or Stop was pressed before the process existed; don't lose it
            self._signal()
        fd = self._proc.stdout.fileno()
        checked = time.monotonic()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            if not self._want and time.monotonic() - checked > 2:
                checked = time.monotonic()
                if not os.path.exists(self.plan["source"]["path"]):
                    # A drive that drops off the bus makes every further read "fail", which would fill
                    # the progress file with false bad areas and waste hours. Stop at once instead.
                    self._want = "lost"
                    self._signal()
            self._tail = (self._tail + chunk.decode("utf-8", "replace"))[-8192:]
            live = ddrescue.parse_status(self._tail)
            if live:
                self._live.update(live)
        code = self._proc.wait()
        self._proc.stdout.close()
        self._map_cache = (0.0, None)  # the pass just wrote its final mapfile
        return code

    def _finish(self, state, message):
        self._map_cache = (0.0, None)
        if self._t0 is not None:
            self._elapsed += max(0.0, self.clock() - self._t0)
            self._t0 = None
        with self._lock:
            self.state = state
            self.message = message
        self._write_jobfile(state)

    # ---- progress --------------------------------------------------------
    def _read_map(self, fresh=False):
        now = self.clock()
        at, cached = self._map_cache
        if cached is not None and not fresh and now - at < 1.0:
            return cached
        try:
            with open(self.plan["mapfile"]) as fh:
                mf = mapfile.parse_mapfile(fh.read())
        except OSError:
            mf = mapfile.Mapfile()
        self._map_cache = (now, mf)
        return mf

    def _summary(self):
        # Decisions are made on a fresh read, never on the cached copy used for the display.
        return mapfile.summarize(self._read_map(fresh=True), self.size)

    def snapshot(self):
        mf = self._read_map()
        sums = mapfile.summarize(mf, self.size)
        finished = sums["finished"]
        if self.state in ("running", "pausing"):
            finished = max(finished, self._live.get("rescued", 0))
        now = self.clock()
        self._samples.append((now, finished))
        speed = self._live.get("rate") if self.state == "running" else 0
        if not speed and len(self._samples) >= 2 and self.state == "running":
            t0, f0 = self._samples[0]
            speed = max(0.0, (finished - f0) / (now - t0)) if now > t0 else 0.0
        if speed:
            self._speed = speed if not self._speed else self._speed * 0.7 + speed * 0.3
        elif self.state != "running":
            self._speed = 0.0
        self._peak = max(self._peak, self._speed)
        slow = (self.state == "running" and self._peak > 0 and 0 < self._speed < self._peak * 0.15
                and self._peak > 5 * 1024 * 1024)
        elapsed = self._elapsed + (max(0.0, now - self._t0) if self._t0 is not None else 0.0)
        remaining = max(0, self.size - finished - sums["skipped"])
        eta = remaining / self._speed if self._speed > 0 else None
        keys = self._phase_keys()
        active = keys[min(self.phase_index, len(keys) - 1)]
        phases = []
        for key, title, note in PHASES:
            if key == "final":
                status = "active" if self.state == "done" else "waiting"
                if self.state == "done":
                    status = "done"
            elif key not in keys:
                status = "skipped"
            elif self.state == "done" or keys.index(key) < self.phase_index:
                status = "done"
            elif key == active and self.state in ("running", "pausing", "paused", "error"):
                status = "active"
            else:
                status = "waiting"
            phases.append({"key": key, "title": title, "note": note, "status": status})
        pass_text = {"quick": "Pass 1 · Quick copy", "retry": "Pass 2 · Gentle retry"}.get(active, "")
        return {
            "state": self.state,
            "message": self.message,
            "phase": active,
            "pass_text": pass_text,
            "percent": round(100.0 * finished / self.size, 1) if self.size else 0.0,
            "finished": finished, "finished_text": human_size(finished),
            "size": self.size, "size_text": human_size(self.size),
            "speed_text": (human_size(self._speed) + "/s") if self._speed else "-",
            "eta_text": human_duration(eta) if self.state == "running" else "-",
            "skipped": sums["skipped"], "skipped_text": human_size(sums["skipped"]),
            "complete": sums["finished"] >= self.size and self.size > 0,
            "elapsed_text": human_duration(elapsed) if elapsed >= 60 else ("under a minute" if elapsed > 0 else "-"),
            "elapsed": int(elapsed),
            "speed_note": ("The drive has slowed right down. That is normal in damaged areas, and a sign it is "
                           "struggling. Keep going, but don't move or knock it.") if slow else "",
            "quick_done": self.phase_index >= 1 or self.state == "done",
            "outlook": (mapfile.outlook(mf, self.size) if (self.phase_index >= 1 or self.state == "done") else None),
            "cells": mapfile.build_cells(mf, self.size, self.cells),
            "phases": phases,
            "image": self.plan["image"],
            "mapfile": self.plan["mapfile"],
            "log": [ln for ln in strip_ansi(self._tail).replace("\r", "\n").splitlines() if ln.strip()][-6:],
        }
