#!/bin/bash
#
# Swap Debrief.app's icon in one command — no full rebuild needed.
#
#   bash launcher/set_icon.sh design/app-icon.png
#   bash launcher/set_icon.sh            # defaults to design/app-icon.png
#
# Pipeline: PNG -> .iconset (all sizes) -> Debrief.icns -> wired into the
# bundle -> Finder/Dock icon cache refreshed.

set -euo pipefail

LAUNCHER_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -P "$LAUNCHER_DIR/.." >/dev/null 2>&1 && pwd)"

SRC="${1:-$REPO_ROOT/design/app-icon.png}"
APP="$REPO_ROOT/Debrief.app"
ICNS="$LAUNCHER_DIR/Debrief.icns"

if [ ! -f "$SRC" ]; then
  echo "error: source PNG not found: $SRC" >&2
  exit 1
fi
if [ ! -d "$APP" ]; then
  echo "error: $APP not found — run launcher/build_app.sh first." >&2
  exit 1
fi

echo "Icon source: $SRC"

# --- PNG -> iconset --------------------------------------------------------
ICONSET="$(mktemp -d)/Debrief.iconset"
mkdir -p "$ICONSET"
for sz in 16 32 128 256 512; do
  sips -z "$sz" "$sz" "$SRC" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
  d=$((sz * 2))
  sips -z "$d" "$d" "$SRC" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
done

# --- iconset -> icns -------------------------------------------------------
iconutil -c icns "$ICONSET" -o "$ICNS"
echo "Built $ICNS"

# --- Wire into the bundle --------------------------------------------------
cp "$ICNS" "$APP/Contents/Resources/Debrief.icns"
/usr/libexec/PlistBuddy -c "Set :CFBundleIconFile Debrief" "$APP/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string Debrief" "$APP/Contents/Info.plist"

# --- Refresh Finder/Dock icon cache ---------------------------------------
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true
# Kick the Dock so a running instance picks up the new icon.
killall Dock >/dev/null 2>&1 || true

echo "Icon set on $APP"
