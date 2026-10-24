#!/bin/sh
# Drive Rescue launcher.
#   sh start.sh            start the program and open it in the browser
#   sh start.sh --check    only check that the tools it needs are present
#   sh start.sh --background   (used by autorun) wait for the desktop before opening the browser
#   sh start.sh --kiosk        (the boot disc) full-screen window, reopened if closed
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=${DRIVE_RESCUE_PORT:-8765}
URL="http://127.0.0.1:$PORT/"
LOG=/tmp/drive-rescue.log
MODE=$1
[ "$MODE" = "--kiosk" ] && export DRIVE_RESCUE_ALLOW_POWER=1   # only the boot disc may shut the computer down

if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then exec sudo sh "$0" "$@"; fi
  echo "Drive Rescue needs administrator rights. Run it as root."; exit 1
fi

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then echo "Python was not found on this system."; exit 1; fi
cd "$HERE" || exit 1
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1   # never write files onto the USB stick

echo "Checking this system..."
"$PY" -m drive_rescue.preflight
READY=$?
[ "$MODE" = "--check" ] && exit $READY
if [ $READY -ne 0 ] && [ "$MODE" != "--kiosk" ]; then
  echo
  echo "Drive Rescue can't start until the item marked MISSING is available."
  echo "Make sure you started from the SystemRescue USB stick."
  exit 1
fi

open_browser() {
  [ -z "$DISPLAY" ] && export DISPLAY=:0
  if [ "$MODE" = "--kiosk" ]; then
    # Never let the screen blank or lock while a long copy runs.
    xset s off -dpms s noblank >/dev/null 2>&1
    rm -f /tmp/drive-rescue-desktop
    PROFILE=/tmp/drive-rescue-profile
    mkdir -p "$PROFILE"
    cat >"$PROFILE/user.js" <<'JSEOF'
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("app.update.auto", false);
user_pref("privacy.fingerprintingProtection", false);
JSEOF
    for b in firefox firefox-esr; do
      if command -v "$b" >/dev/null 2>&1; then
        # If the window is closed by accident, bring it straight back.
        # (unless the person chose "Go to the desktop", which leaves a flag file behind)
        while [ ! -e /tmp/drive-rescue-desktop ] && { kill -0 $SERVER 2>/dev/null || [ -z "$SERVER" ]; }; do
          "$b" --kiosk --no-remote --profile "$PROFILE" "$URL" >/dev/null 2>&1
          sleep 2
        done
        return 0
      fi
    done
    return 1
  fi
  for b in firefox firefox-esr chromium google-chrome; do
    if command -v "$b" >/dev/null 2>&1; then
      "$b" "$URL" >/dev/null 2>&1 &
      return 0
    fi
  done
  return 1
}

wait_for_desktop() {
  # Up to two minutes for the graphical desktop to appear.
  n=0
  while [ $n -lt 120 ]; do
    [ -S /tmp/.X11-unix/X0 ] && return 0
    n=$((n + 1)); sleep 1
  done
  return 1
}

if "$PY" - "$PORT" <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYEOF
then
  echo "Drive Rescue is already running."
else
  "$PY" -m drive_rescue --no-browser --port "$PORT" >"$LOG" 2>&1 &
  SERVER=$!
  trap 'kill $SERVER 2>/dev/null' INT TERM
  sleep 2
  if ! kill -0 $SERVER 2>/dev/null; then echo "Drive Rescue could not start:"; cat "$LOG"; exit 1; fi
fi

echo
echo "Drive Rescue is running. Open this address in a browser if one doesn't open by itself:"
echo "  $URL"
if [ "$MODE" = "--background" ]; then wait_for_desktop; fi
open_browser || echo "(No browser was found. Open the address above from any browser.)"
[ -n "$SERVER" ] && wait $SERVER
