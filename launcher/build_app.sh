#!/bin/bash
#
# Build Debrief.app at the repo root.
#
# A hand-rolled .app bundle (not osacompile) is used on purpose: it gives us a
# STABLE CFBundleIdentifier, so the one-time macOS permission grants (Calendar,
# Mail, Screen Recording) survive rebuilds instead of resetting every time.
#
# The bundle is a thin wrapper: its executable just runs the committed
# launcher/debrief-launch.sh, which is the real brain.
#
# Usage:  bash launcher/build_app.sh

set -euo pipefail

LAUNCHER_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -P "$LAUNCHER_DIR/.." >/dev/null 2>&1 && pwd)"

APP="$REPO_ROOT/Debrief.app"
LAUNCH_SCRIPT="$LAUNCHER_DIR/debrief-launch.sh"
ICNS="$LAUNCHER_DIR/Debrief.icns"
BUNDLE_ID="com.meora.debrief"

echo "Building $APP"

# Make sure the launcher is executable.
chmod +x "$LAUNCH_SCRIPT"

# Fresh bundle skeleton.
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# --- Executable stub -------------------------------------------------------
# Runs the real launcher by absolute path so it works no matter where macOS
# launches the bundle from (Finder, Dock, `open`).
cat > "$APP/Contents/MacOS/Debrief" <<STUB
#!/bin/bash
exec "$LAUNCH_SCRIPT"
STUB
chmod +x "$APP/Contents/MacOS/Debrief"

# --- Icon ------------------------------------------------------------------
ICON_PLIST_ENTRY=""
if [ -f "$ICNS" ]; then
  cp "$ICNS" "$APP/Contents/Resources/Debrief.icns"
  ICON_PLIST_ENTRY="	<key>CFBundleIconFile</key>
	<string>Debrief</string>"
  echo "Wired icon: $ICNS"
else
  echo "No icon at $ICNS yet — bundle built without a custom icon."
fi

# --- Info.plist ------------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>Debrief</string>
	<key>CFBundleDisplayName</key>
	<string>Debrief</string>
	<key>CFBundleIdentifier</key>
	<string>$BUNDLE_ID</string>
	<key>CFBundleVersion</key>
	<string>1</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0</string>
	<key>CFBundleExecutable</key>
	<string>Debrief</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
$ICON_PLIST_ENTRY
	<key>LSMinimumSystemVersion</key>
	<string>12.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.productivity</string>
</dict>
</plist>
PLIST

# --- PkgInfo ---------------------------------------------------------------
printf 'APPL????' > "$APP/Contents/PkgInfo"

# --- Refresh Finder/Dock icon cache ---------------------------------------
touch "$APP"
# Nudge Launch Services so the new icon/identity is registered.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true

echo "Built $APP"
