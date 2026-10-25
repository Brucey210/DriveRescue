#!/usr/bin/env python3
"""Boot the finished ISO in a virtual machine and look at the screen.

    python3 tests/boot_test.py DriveRescue.iso bios|uefi OUTDIR

Takes screenshots of the virtual screen while it starts, reads the words off them (OCR), and checks that:
  * no boot menu or SystemRescue wording ever appears,
  * Drive Rescue's own first screen shows up.
Exit status 0 only when both hold. Screenshots are left in OUTDIR so they can be looked at.
Needs qemu-system-x86_64, tesseract, Pillow, and (for UEFI) OVMF.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

FORBIDDEN = ("systemrescue", "system-rescue", "system rescue", "boot systemrescue", "memtest", "copytoram")
WANTED = ("what would you like to do", "rescue files from a failing drive")


def ovmf_args():
    for code in ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/ovmf/OVMF.fd"):
        if os.path.exists(code):
            if code.endswith("OVMF.fd"):
                return ["-bios", code]
            return ["-drive", f"if=pflash,format=raw,readonly=on,file={code}"]
    return None


def monitor(sock_path, command, wait=1.0):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(10)
    s.connect(sock_path)
    time.sleep(0.2)
    s.sendall((command + "\n").encode())
    time.sleep(wait)
    s.close()


def ocr(png):
    out = subprocess.run(["tesseract", png, "-", "--psm", "6"], capture_output=True, text=True)
    return out.stdout.lower()


def run(iso, mode, outdir, total_seconds=420, tick=10):
    from PIL import Image
    os.makedirs(outdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="bt-")
    sock = os.path.join(tmp, "mon")
    cmd = ["qemu-system-x86_64", "-m", "3072", "-smp", "2", "-cdrom", iso, "-boot", "d", "-display", "none",
           "-vga", "std", "-monitor", f"unix:{sock},server,nowait", "-serial", "null", "-net", "none"]
    if os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.W_OK):
        cmd += ["-enable-kvm", "-cpu", "host"]
    if mode == "uefi":
        extra = ovmf_args()
        if not extra:
            return {"mode": mode, "ok": False, "error": "OVMF (UEFI firmware) is not installed"}
        cmd += extra
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    result = {"mode": mode, "ok": False, "shots": [], "forbidden": [], "found": False, "kvm": "-enable-kvm" in cmd}
    start = time.time()
    try:
        while not os.path.exists(sock) and time.time() - start < 20:
            time.sleep(0.5)
        n = 0
        while time.time() - start < total_seconds:
            time.sleep(tick)
            if proc.poll() is not None:
                result["error"] = "the virtual machine stopped: " + proc.stderr.read().decode()[-300:]
                break
            ppm = os.path.join(tmp, f"s{n}.ppm")
            monitor(sock, f"screendump {ppm}")
            if not os.path.exists(ppm):
                continue
            png = os.path.join(outdir, f"{mode}-{n:02d}.png")
            Image.open(ppm).convert("RGB").save(png)
            text = ocr(png)
            result["shots"].append({"file": os.path.basename(png), "t": int(time.time() - start), "text": " ".join(text.split())[:200]})
            for word in FORBIDDEN:
                if word in text and word not in result["forbidden"]:
                    result["forbidden"].append(word)
            if any(w in text for w in WANTED):
                result["found"] = True
                break
            n += 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    result["ok"] = result["found"] and not result["forbidden"]
    return result


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    res = run(sys.argv[1], sys.argv[2], sys.argv[3])
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
