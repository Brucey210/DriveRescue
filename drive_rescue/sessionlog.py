"""A running record of what the program did, kept as a plain text file for a technician."""
import threading
import time

MAX_LINES = 5000


class SessionLog:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lines = []
        self.paths = []
        self._lock = threading.Lock()

    def add_path(self, path):
        """Also keep the log in this file (on the healthy drive, next to the copy)."""
        with self._lock:
            if path in self.paths:
                return
            self.paths.append(path)
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(self.lines) + ("\n" if self.lines else ""))
            except OSError:
                self.paths.remove(path)

    def log(self, message):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.clock()))}  {message}"
        with self._lock:
            self.lines.append(line)
            del self.lines[:-MAX_LINES]
            for path in list(self.paths):
                try:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    self.paths.remove(path)  # the drive went away or is read-only; the rest still works

    def text(self):
        with self._lock:
            return "\n".join(self.lines) + ("\n" if self.lines else "")
