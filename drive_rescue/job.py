"""Running the copy: ddrescue in passes, with pause, resume and stop."""
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque

from . import ddrescue, mapfile, partitions
from . import drives as drives_mod
from .util import human_duration, human_size, strip_ansi

CELLS = 294
META_MAX_AREA = 64 * 1024 * 1024
META_MFT_AREA = 128 * 1024 * 1024
META_TOTAL_CAP = 1024 * 1024 * 1024
HOT_C = 60  # degrees: above this the drive is paused to cool
PHASES = [
    ("meta", "Key areas first", "Partition tables and file indexes"),
    ("quick", "Quick copy", "Grabbing everything that reads easily"),
    ("retry", "Gentle retry", "Going back for skipped areas, slowly"),
    ("reverse", "Backwards retry", "Reading what is left from the other end"),
    ("final", "Final check", "Making sure the copy is complete"),
]


class JobError(Exception):
    pass


class ImageJob:
    META_FIRST = True

    def __init__(self, system, plan, opts, binary=None, cells=CELLS, clock=time.time, log=None):
        self.system = system
        self.plan = plan
        self.opts = {"style": "gentle", "retries": 3, "direction": "forward", **(opts or {})}
        self.binary = binary or system.which("ddrescue") or system.which("gddrescue")
        self.cells = cells
        self.clock = clock
        self.log = log or (lambda message: None)
        self.stall_limit = 600.0   # seconds without any progress before pausing
        self.heat_interval = 120.0  # seconds between temperature checks
        self.watch_tick = 1.0
        self.pause_reason = ""
        self._inhibit = None
        self._power = (0.0, {"on_battery": False, "percent": None})
        self._speeds = deque(maxlen=90)
        self._auto = None          # (reason, message) when the program pauses by itself
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
            self.log("Copy started" + (f" ({self.plan['source'].get('model') or ''} {self.plan['source'].get('size_text') or ''})".rstrip()
                                       if self.plan.get("source") else ""))
            self._begin_thread()

    def resume(self):
        with self._lock:
            if self.state in ("paused", "error", "stopped"):
                self._verify_source()
                self.log("Resumed")
                self._begin_thread()

    def _verify_source(self):
        """Before carrying on from saved progress, make sure the drive is still the same one.
        A different drive at the same place would be mixed into the copy and spoil it."""
        want = [self.plan.get("source")]
        if self.plan.get("dest_drive"):
            want.append(self.plan["dest_drive"])
        try:
            current = {d["path"]: d for d in drives_mod.list_drives(self.system)}
        except Exception:
            return  # can't look; the copy itself will fail clearly if the drive is gone
        for drive in want:
            if not drive or not drive.get("path"):
                continue
            now = current.get(drive["path"])
            if now is None and drive.get("serial"):
                # unplugged and plugged back in: the system often gives it a new name, so find it by serial number
                now = next((d for d in current.values() if d.get("serial") == drive["serial"] and d["size"] == drive["size"]), None)
                if now is not None:
                    self.log(f"The drive came back as {now['path']} (it was {drive['path']}); carrying on with it")
                    if drive is self.plan.get("source"):
                        self.plan["source"] = now
                        self.system.set_readonly(now["path"])  # the new name has not been locked yet
                    elif drive is self.plan.get("dest_drive"):
                        self.plan["dest_drive"] = now
                        self.plan["image"] = now["path"]
                    continue
            if now is None:
                raise JobError("The drive isn't connected any more. Plug it back in, wait a moment, and try again.")
            same_serial = not drive.get("serial") or not now.get("serial") or drive["serial"] == now["serial"]
            if now["size"] != drive["size"] or not same_serial:
                raise JobError("The drive at that place is not the same one the copy started with, so carrying on "
                               "would mix two drives together. Put the original drive back, or start a new copy.")

    def pause(self):
        with self._lock:
            if self.state == "running":
                self.log("Paused by the person")
                self._want = "pause"
                self.state = "pausing"
                self._signal()

    def stop(self):
        with self._lock:
            if self.state in ("running", "pausing"):
                self.log("Stopped by the person")
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
            self.log(f"WARNING: the failing drive could not be locked as read-only ({err})")
        elif self.system.is_readonly(src["path"]):
            self.log(f"The failing drive {src['path']} was locked as read-only; the system will refuse any write to it")
        else:
            self.log(f"WARNING: the failing drive {src['path']} did not confirm it is read-only")
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
        self._auto = None
        self.pause_reason = ""
        self._t0 = self.clock()
        self.state = "running"
        self.message = ""
        if self._inhibit is None:
            self._inhibit = self.system.inhibit_sleep()  # no sleeping or screen locking mid-copy
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- the worker ------------------------------------------------------
    def _phase_keys(self):
        keys = ["meta", "quick"] if (self.META_FIRST and self.opts.get("meta_first", True)) else ["quick"]
        if int(self.opts.get("retries", 3)) > 0:
            keys.append("retry")
            if self.opts.get("thorough"):
                keys.append("reverse")
        return keys

    def _run(self):
        try:
            keys = self._phase_keys()
            while self.phase_index < len(keys):
                key = keys[self.phase_index]
                if key in ("retry", "reverse") and self._summary()["skipped"] == 0:
                    self.phase_index += 1
                    continue
                self.log(f"Starting: {dict((k, t) for k, t, _n in PHASES).get(key, key)}")
                rc = self._run_meta() if key == "meta" else self._run_pass(key)
                if rc != 0 and not self._want and not os.path.exists(self.plan["source"]["path"]):
                    self._want = "lost"  # it went away between two checks
                if self._want == "pause":
                    if self._auto:
                        self.pause_reason, note = self._auto
                        self._finish("paused", note)
                    else:
                        self._finish("paused", "Paused. Your progress is saved.")
                    return
                if self._want == "stop":
                    self._finish("stopped", "Stopped. What was copied so far is kept.")
                    return
                if self._want == "lost":
                    self._finish("error", "The failing drive disappeared. It may have been unplugged, or it stopped "
                                          "responding. Check the cable and power, plug it back in, then press Resume. "
                                          "It carries on from where it stopped; nothing is lost.")
                    return
                self.log(f"ddrescue finished the pass with code {rc}")
                if rc != 0:
                    tail = strip_ansi(self._tail).strip().splitlines()[-1:] or [""]
                    self._finish("error", f"The copy stopped unexpectedly (code {rc}). The drive may have stopped "
                                          f"responding. Your progress is saved, so you can resume. "
                                          f"{ddrescue.explain_exit(rc)} {tail[0][:160]}".replace("  ", " ").strip())
                    return
                self.phase_index += 1
                if key == "meta":
                    continue
                if (key == "quick" and self.opts.get("pause_after_quick") and self.phase_index < len(keys)
                        and self._summary()["skipped"] > 0):
                    self._finish("paused", "The quick copy is finished. Everything that reads easily is now safe in the "
                                           "image, so you can get your important files out now. Press Resume to go "
                                           "back for the difficult parts.")
                    return
            self._complete()
        except Exception as exc:  # never let the thread die silently
            self._finish("error", f"Unexpected problem: {exc}")

    def _command(self, key, area=None):
        return ddrescue.build_command(self.binary, self.plan["source"]["path"], self.plan["image"],
                                      self.plan["mapfile"], key, {**self.opts, **self.features, "area": area},
                                      sector=self.plan.get("sector", 512))

    # ---- the key areas of the drive, copied before everything else
    def _meta_areas(self):
        """Stretches that hold the drive's own maps: the partition tables, the start of every partition
        (boot records, allocation tables, the first folders) and the NTFS file index. If the drive dies
        part way through, these are what make the rest of the copy readable."""
        path, size = self.plan["source"]["path"], self.size
        sector = self.plan.get("sector", 512) or 512
        mib = 1024 * 1024
        areas = [(0, min(mib, size)), (max(0, size - mib), min(mib, size))]
        try:
            layout = partitions.read_layout(path, sector)
        except OSError:
            layout = {"parts": []}
        for p in layout.get("parts", []):
            start, plen = p["start"] * sector, (p["end"] - p["start"] + 1) * sector
            areas.append((start, min(META_MAX_AREA, plen)))
            try:
                boot = partitions._read(path, start, 512)
            except OSError:
                continue
            if boot[3:11] == b"NTFS    " and len(boot) >= 56:
                bps = int.from_bytes(boot[11:13], "little")
                spc = boot[13]
                mft = int.from_bytes(boot[48:56], "little") * bps * spc
                if bps and spc and 0 < mft < plen:
                    areas.append((start + mft, min(META_MFT_AREA, plen - mft)))
        grain = max(4096, sector)
        spans = []
        for pos, length in sorted(areas):
            lo = pos // grain * grain
            hi = min(size, -(-(pos + length) // grain) * grain)
            if spans and lo <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], hi)
            else:
                spans.append([lo, hi])
        out, total = [], 0
        for lo, hi in spans:
            if hi <= lo:
                continue
            take = min(hi - lo, META_TOTAL_CAP - total)
            if take <= 0:
                break
            out.append((lo, take))
            total += take
        return out

    def _run_meta(self):
        for area in self._meta_areas():
            rc = self._run_pass("meta", area)
            if self._want or rc != 0:
                return rc
        return 0

    def _complete(self):
        self._finish("done", "")

    def _run_pass(self, key, area=None):
        cmd = self._command(key, area)
        self._tail = ""
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        if self._want:  # Pause or Stop was pressed before the process existed; don't lose it
            self._signal()
        threading.Thread(target=self._watch, args=(self._proc,), daemon=True).start()
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

    def _watch(self, proc):
        """Looks after the drive while a pass runs: pauses by itself when nothing has moved for a long
        time, or when the drive gets too hot. Everything is saved, so the person can simply resume."""
        last_progress, last_change = None, time.monotonic()
        next_heat = time.monotonic() + self.heat_interval
        while proc.poll() is None and not self._want:
            time.sleep(self.watch_tick)
            if proc.poll() is not None or self._want:
                return
            now = time.monotonic()
            progress = (self._live.get("rescued"), self._live.get("errsize"), self._live.get("errors"))
            if progress != last_progress:
                last_progress, last_change = progress, now
            elif now - last_change >= self.stall_limit:
                minutes = max(1, int(self.stall_limit // 60))
                self._auto_pause("stall", f"The drive stopped responding for about {minutes} "
                                          f"{'minute' if minutes == 1 else 'minutes'}, so the copy paused to protect it. "
                                          "Your progress is saved. Check the cable and power, then press Resume.")
                return
            if now >= next_heat:
                next_heat = now + self.heat_interval
                temp = drives_mod.drive_temperature(self.system, self.plan["source"]["path"])
                if temp is not None and temp >= HOT_C:
                    self._auto_pause("heat", f"The drive is hot ({temp} °C), so the copy paused to protect it. "
                                             "Your progress is saved. Let it cool for ten minutes, then press Resume.")
                    return

    def _power_now(self):
        at, cached = self._power
        now = time.monotonic()
        if now - at > 10:
            try:
                cached = self.system.power_status()
            except Exception:
                cached = {"on_battery": False, "percent": None}
            self._power = (now, cached)
        return cached

    def _auto_pause(self, reason, message):
        with self._lock:
            if self._want or self.state != "running":
                return
            self.log(f"Paused automatically ({reason}): {message}")
            self._auto = (reason, message)
            self._want = "pause"
            self.state = "pausing"
            self._signal()

    def _finish(self, state, message):
        self.log(f"Now {state}" + (f": {message}" if message else ""))
        if self._inhibit is not None and state != "pausing":
            self._inhibit.close()
            self._inhibit = None
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
        if self.state == "running" and speed:
            self._speeds.append(speed)  # the raw speeds, so the spread is not smoothed away
        eta_text = human_duration(eta) if self.state == "running" else "-"
        if self.state == "running" and len(self._speeds) >= 15 and remaining > 0:
            ordered = sorted(self._speeds)
            slow_s, fast_s = ordered[len(ordered) // 4], ordered[(3 * len(ordered)) // 4]
            if slow_s > 0 and fast_s / slow_s > 1.4:  # the speed is all over the place: say so honestly
                low, high = remaining / fast_s, remaining / slow_s
                if human_duration(low) != human_duration(high):
                    eta_text = f"{human_duration(low)} to {human_duration(high)}"
        keys = self._phase_keys()
        active = keys[min(self.phase_index, len(keys) - 1)]
        phases = []
        for key, title, note in PHASES:
            if key == "final":
                status = "active" if self.state == "done" else "waiting"
                if self.state == "done":
                    status = "done"
            elif key not in keys:
                if key == "reverse":
                    continue  # only shown when it was asked for
                status = "skipped"
            elif self.state == "done" or keys.index(key) < self.phase_index:
                status = "done"
            elif key == active and self.state in ("running", "pausing", "paused", "error"):
                status = "active"
            else:
                status = "waiting"
            phases.append({"key": key, "title": title, "note": note, "status": status})
        pass_text = {"meta": "Pass 0 · Key areas", "quick": "Pass 1 · Quick copy", "retry": "Pass 2 · Gentle retry", "reverse": "Pass 3 · Backwards retry"}.get(active, "")
        return {
            "state": self.state,
            "message": self.message,
            "phase": active,
            "pass_text": pass_text,
            "percent": round(100.0 * finished / self.size, 1) if self.size else 0.0,
            "finished": finished, "finished_text": human_size(finished),
            "size": self.size, "size_text": human_size(self.size),
            "speed_text": (human_size(self._speed) + "/s") if self._speed else "-",
            "eta_text": eta_text,
            "power": self._power_now(),
            "skipped": sums["skipped"], "skipped_text": human_size(sums["skipped"]),
            "complete": sums["finished"] >= self.size and self.size > 0,
            "elapsed_text": human_duration(elapsed) if elapsed >= 60 else ("under a minute" if elapsed > 0 else "-"),
            "elapsed": int(elapsed),
            "speed_note": ("The drive has slowed right down. That is normal in damaged areas, and a sign it is "
                           "struggling. Keep going, but don't move or knock it.") if slow else "",
            "quick_done": (("quick" in keys and keys.index("quick") < self.phase_index) or self.state == "done"),
            "pause_reason": self.pause_reason,
            "outlook": (mapfile.outlook(mf, self.size) if (("quick" in keys and keys.index("quick") < self.phase_index) or self.state == "done") else None),
            "cells": mapfile.build_cells(mf, self.size, self.cells),
            "phases": phases,
            "image": self.plan["image"],
            "mapfile": self.plan["mapfile"],
            "log": [ln for ln in strip_ansi(self._tail).replace("\r", "\n").splitlines() if ln.strip()][-6:],
        }
