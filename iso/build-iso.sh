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
# A launcher so Drive Rescue can be opened again after leaving it for the desktop.
mkdir -p "$WORK/root/usr/share/applications" "$WORK/root/usr/share/pixmaps" "$WORK/root/root/Desktop"
cp "$ROOT/assets/brand/mark-256.png" "$WORK/root/usr/share/pixmaps/drive-rescue.png"
cat > "$WORK/root/usr/share/applications/drive-rescue-open.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=Drive Rescue
Comment=Rescue files from a failing drive, or clone a drive
Exec=sh /opt/drive-rescue/start.sh --kiosk
Icon=drive-rescue
Terminal=false
Categories=System;
DESK
cp "$WORK/root/usr/share/applications/drive-rescue-open.desktop" "$WORK/root/root/Desktop/Drive Rescue.desktop"
chmod 755 "$WORK/root/root/Desktop/Drive Rescue.desktop"
# Make it look like Drive Rescue everywhere, not like the system underneath.
BRAND="$ROOT/assets/brand"
cat > "$WORK/root/etc/os-release" <<'OSR'
NAME="Drive Rescue"
ID=sysrescue
ID_LIKE=arch
PRETTY_NAME="Drive Rescue"
ANSI_COLOR="0;36"
OSR
cat > "$WORK/root/etc/issue" <<'ISSUE'

  Drive Rescue
  If the program did not open by itself, type:  startx

ISSUE
rm -f "$WORK/root/etc/motd"
# Desktop wallpaper: every stock wallpaper becomes the Drive Rescue one, and the desktop is told to use it.
mkdir -p "$WORK/root/usr/share/backgrounds/drive-rescue"
cp "$BRAND/wallpaper-1920x1080.png" "$WORK/root/usr/share/backgrounds/drive-rescue/wallpaper.png"
for f in "$WORK"/root/usr/share/backgrounds/xfce/*; do
  case "$f" in
    *.svg) cp "$BRAND/wallpaper-1920x1080.svg" "$f" ;;
    *.jpg) cp "$BRAND/wallpaper-1920x1080.jpg" "$f" ;;
    *.png) cp "$BRAND/wallpaper-1920x1080.png" "$f" ;;
  esac
done
XFCE_XML="$WORK/root/root/.config/xfce4/xfconf/xfce-perchannel-xml"
mkdir -p "$XFCE_XML"
{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<channel name="xfce4-desktop" version="1.0">'
  echo '  <property name="backdrop" type="empty"><property name="screen0" type="empty">'
  for m in monitor0 monitorVirtual-1 monitorVGA-1 monitorHDMI-1 monitoreDP-1 monitorDP-1 monitorHDMI-A-1 monitorDisplayPort-0; do
    echo "    <property name=\"$m\" type=\"empty\"><property name=\"workspace0\" type=\"empty\">"
    echo '      <property name="color-style" type="int" value="0"/><property name="image-style" type="int" value="5"/>'
    echo '      <property name="last-image" type="string" value="/usr/share/backgrounds/drive-rescue/wallpaper.png"/>'
    echo '    </property></property>'
  done
  echo '  </property></property>'
  echo '</channel>'
} > "$XFCE_XML/xfce4-desktop.xml"
# Hide the "Drive Rescue is built on ..." trace in the About box of the hardware tool.
rm -f "$WORK/root/usr/share/hardinfo2/pixmaps/distros/sysrescue.svg" 2>/dev/null || true

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
# One silent boot entry: no menu, no choices, straight into Drive Rescue (graphical only).
# "Esc" (UEFI) or "Shift" (BIOS) still reaches a safe-graphics entry for computers that show nothing.
CMDLINE="archisobasedir=sysresccd archisolabel=RESCUE1302 iomem=relaxed quiet loglevel=0 vt.global_cursor_default=0 systemd.show_status=0 rd.udev.log_level=0"
mkdir -p "$WORK/iso_add/boot/grub" "$WORK/iso_add/syslinux"
cat > "$WORK/iso_add/boot/grub/grubsrcd.cfg" <<GRUB
set timeout=0
set timeout_style=hidden
set default=0
set pager=0
if loadfont /boot/grub/font.pf2 ; then
	set gfxmode=1024x768,auto
	insmod efi_gop
	insmod efi_uga
	insmod gfxterm
	insmod all_video
	insmod png
	terminal_output gfxterm
	background_image -m stretch /boot/grub/splash.png
fi
menuentry 'Drive Rescue' {
	set gfxpayload=keep
	linux /sysresccd/boot/x86_64/vmlinuz $CMDLINE
	initrd /sysresccd/boot/intel_ucode.img /sysresccd/boot/amd_ucode.img /sysresccd/boot/x86_64/sysresccd.img
}
menuentry 'Drive Rescue (safe graphics)' {
	linux /sysresccd/boot/x86_64/vmlinuz $CMDLINE nomodeset
	initrd /sysresccd/boot/intel_ucode.img /sysresccd/boot/amd_ucode.img /sysresccd/boot/x86_64/sysresccd.img
}
GRUB
: > "$WORK/iso_add/boot/grub/custom.cfg"
cat > "$WORK/iso_add/syslinux/sysresccd.cfg" <<SYS
DEFAULT sys
LABEL sys
CONFIG boot/syslinux/sysresccd_sys.cfg
SYS
cat > "$WORK/iso_add/syslinux/sysresccd_sys.cfg" <<SYS
UI boot/syslinux/vesamenu.c32
MENU TITLE Drive Rescue
MENU BACKGROUND splash.png
MENU HIDDEN
MENU AUTOBOOT Starting Drive Rescue
NOESCAPE 1
TIMEOUT 5
TOTALTIMEOUT 5
DEFAULT drive

LABEL drive
MENU LABEL Drive Rescue
LINUX boot/x86_64/vmlinuz
INITRD boot/intel_ucode.img,boot/amd_ucode.img,boot/x86_64/sysresccd.img
APPEND $CMDLINE

LABEL safe
MENU LABEL Drive Rescue (safe graphics)
LINUX boot/x86_64/vmlinuz
INITRD boot/intel_ucode.img,boot/amd_ucode.img,boot/x86_64/sysresccd.img
APPEND $CMDLINE nomodeset
SYS
cp "$BRAND/splash-1024x768.png" "$WORK/iso_add/boot/grub/splash.png"
cp "$BRAND/splash-640x480.png" "$WORK/iso_add/syslinux/splash.png"
MAPS=(-map "$WORK/new.sfs" "$SFS"
  -map "$WORK/iso_add/boot/grub/grubsrcd.cfg" /boot/grub/grubsrcd.cfg
  -map "$WORK/iso_add/boot/grub/custom.cfg" /boot/grub/custom.cfg
  -map "$WORK/iso_add/boot/grub/splash.png" /boot/grub/splash.png
  -map "$WORK/iso_add/syslinux/sysresccd.cfg" /sysresccd/boot/syslinux/sysresccd.cfg
  -map "$WORK/iso_add/syslinux/sysresccd_sys.cfg" /sysresccd/boot/syslinux/sysresccd_sys.cfg
  -map "$WORK/iso_add/syslinux/splash.png" /sysresccd/boot/syslinux/splash.png -map "$WORK/iso_add/sysrescue.d/500-driverescue.yaml" /sysrescue.d/500-driverescue.yaml)
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
mkdir -p "$WORK/verify"
xorriso -osirrox on -indev "$OUT" -extract /boot/grub/grubsrcd.cfg "$WORK/verify/grub.cfg" >/dev/null 2>&1
grep -q "Drive Rescue" "$WORK/verify/grub.cfg" && ! grep -qi "systemrescue" "$WORK/verify/grub.cfg" || die "check failed: boot menu was not replaced"
[ "$(grep -c '^menuentry' "$WORK/verify/grub.cfg")" -le 2 ] || die "check failed: boot menu has too many entries"
xorriso -indev "$OUT" -toc 2>&1 | grep -qi "boot" || echo "    (note: could not confirm boot records, test the ISO in a virtual machine)"
sha256sum "$OUT" | tee "$OUT.sha256"
echo
echo "Done: $OUT ($(du -h "$OUT" | cut -f1))"
