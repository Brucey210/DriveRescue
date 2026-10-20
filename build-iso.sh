#!/bin/bash
# Build DriveRescue.iso: SystemRescue with Drive Rescue built in, starting by itself.
#
#   sudo bash iso/build-iso.sh systemrescue-13.02-amd64.iso [DriveRescue.iso]
#
# Needs a Linux computer with: xorriso, squashfs-tools. Run as root (squashfs needs it).
# The original ISO is never changed. The boot setup (BIOS and UEFI) is copied across as it is.
set -euo pipefail

BASE=${1:-}
OUT=${2:-DriveRescue.iso}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")

die() { echo "ERROR: $*" >&2; exit 1; }
[ -n "$BASE" ] && [ -f "$BASE" ] || die "usage: sudo bash iso/build-iso.sh <systemrescue.iso> [output.iso]"
[ "$(id -u)" = 0 ] || die "run this as root (use sudo)"
for tool in xorriso unsquashfs mksquashfs sha512sum; do
  command -v "$tool" >/dev/null || die "$tool is missing. On Ubuntu/Debian: sudo apt install xorriso squashfs-tools"
done
[ -f "$ROOT/drive_rescue/__init__.py" ] && [ -f "$ROOT/usb/start.sh" ] || die "run from the project folder (drive_rescue/ and usb/ not found)"

WORK=$(mktemp -d /tmp/driverescue-build.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
step() { echo; echo "==> $*"; }

step "Finding the system image inside $BASE"
SFS=$(xorriso -indev "$BASE" -find / -name airootfs.sfs 2>/dev/null | tr -d "'" | grep '^/' | head -1 || true)
[ -n "$SFS" ] || die "could not find airootfs.sfs in that ISO. Is it a SystemRescue ISO?"
echo "    found $SFS"
SUM="${SFS%.sfs}.sha512"
HAVE_SUM=$(xorriso -indev "$BASE" -find / -name "$(basename "$SUM")" 2>/dev/null | tr -d "'" | grep '^/' | head -1 || true)

step "Unpacking the system image (a few minutes)"
xorriso -osirrox on -indev "$BASE" -extract "$SFS" "$WORK/airootfs.sfs" >/dev/null 2>&1
[ -s "$WORK/airootfs.sfs" ] || die "could not extract the system image"
INFO=$(unsquashfs -s "$WORK/airootfs.sfs")
COMP=$(echo "$INFO" | sed -n 's/^Compression //p' | head -1)
BLOCK=$(echo "$INFO" | sed -n 's/^Block size //p' | head -1)
echo "    compression: ${COMP:-unknown}, block size: ${BLOCK:-unknown}"
unsquashfs -no-progress -d "$WORK/root" "$WORK/airootfs.sfs" >/dev/null

step "Adding Drive Rescue"
DEST="$WORK/root/opt/drive-rescue"
mkdir -p "$DEST"
cp -a "$ROOT/drive_rescue" "$DEST/drive_rescue"
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$ROOT/usb/start.sh" "$DEST/start.sh"
chmod 755 "$DEST/start.sh"
# Start in the desktop session, full screen. (The desktop starts by itself, see sysrescue.d below.)
mkdir -p "$WORK/root/etc/xdg/autostart"
cat > "$WORK/root/etc/xdg/autostart/drive-rescue.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=Drive Rescue
Comment=Rescue files from a failing drive, or clone a drive
Exec=sh /opt/drive-rescue/start.sh --kiosk
Terminal=false
X-GNOME-Autostart-enabled=true
DESK
# A note for anyone who ends up at the text console instead of the desktop.
cat >> "$WORK/root/etc/motd" <<'MOTD'

  Drive Rescue is installed on this disc. To start it:   sh /opt/drive-rescue/start.sh --kiosk
  (or type  startx  to open the desktop, where it starts by itself)

MOTD

step "Packing the system image again (this is the slow part, 10 to 20 minutes)"
SQ_OPTS=(-no-progress -noappend)
[ -n "${COMP:-}" ] && SQ_OPTS+=(-comp "$COMP")
[ -n "${BLOCK:-}" ] && SQ_OPTS+=(-b "$BLOCK")
mksquashfs "$WORK/root" "$WORK/new.sfs" "${SQ_OPTS[@]}" >/dev/null
[ -s "$WORK/new.sfs" ] || die "packing failed"
rm -rf "$WORK/root"

step "Writing the new ISO"
mkdir -p "$WORK/iso_add/sysrescue.d"
cat > "$WORK/iso_add/sysrescue.d/500-driverescue.yaml" <<'YAML'
---
global:
    dostartx: true
YAML
MAPS=(-map "$WORK/new.sfs" "$SFS" -map "$WORK/iso_add/sysrescue.d/500-driverescue.yaml" /sysrescue.d/500-driverescue.yaml)
if [ -n "$HAVE_SUM" ]; then
  echo "$(sha512sum "$WORK/new.sfs" | cut -d' ' -f1)  $(basename "$SFS")" > "$WORK/new.sha512"
  MAPS+=(-map "$WORK/new.sha512" "$HAVE_SUM")
fi
rm -f "$OUT"
xorriso -indev "$BASE" -outdev "$OUT" -boot_image any replay "${MAPS[@]}" -commit >"$WORK/xorriso.log" 2>&1 \
  || { cat "$WORK/xorriso.log"; die "could not write the ISO"; }

step "Checking the result"
[ -s "$OUT" ] || die "the ISO was not created"
xorriso -indev "$OUT" -find /sysrescue.d -name 500-driverescue.yaml 2>/dev/null | grep -q 500-driverescue || die "check failed: settings missing"
xorriso -indev "$OUT" -toc 2>&1 | grep -qi "boot" || echo "    (note: could not confirm boot records, test the ISO in a virtual machine)"
sha256sum "$OUT" | tee "$OUT.sha256"
echo
echo "Done: $OUT ($(du -h "$OUT" | cut -f1))"
