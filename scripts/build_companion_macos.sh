#!/bin/zsh
# Builds "Knovent Companion.app" for macOS (the architecture of the machine this runs on) as a proper onedir app bundle, and a clean ZIP for the website's download button.
#
#   scripts/build_companion_macos.sh https://your-site.vercel.app [more origins...]
#
# Environment (all optional; nothing is ever stored in this file or in the repository):
#   KNOVENT_VERSION          version in the bundle and the file name (default: the package version)
#   KNOVENT_SIGN_IDENTITY    the exact name of a "Developer ID Application: ..." identity in YOUR login keychain (see: security find-identity -v -p codesigning).
#                            Unset = an ad-hoc signed development build (it runs here; other Macs will refuse it until it is signed and notarized).
#   KNOVENT_NOTARY_PROFILE   the name of a notarytool keychain profile you created with `xcrun notarytool store-credentials` (Apple ID or API key stay in the keychain).
#                            Needs KNOVENT_SIGN_IDENTITY. Unset = the build is NOT notarized and says so.
# The website addresses are written into the build: the companion pairs by itself only with them.
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -ge 1 ] || { echo "Give your website address, e.g. https://your-site.vercel.app"; exit 1; }
VERSION="${KNOVENT_VERSION:-$(python3 -c 'import re;print(re.search(r"__version__ = \"([^\"]+)\"", open("src/faqengine/__init__.py").read()).group(1))')}"
ARCH="$(uname -m)"; [ "$ARCH" = "x86_64" ] && TAG="x64" || TAG="arm64"
BUILD=build/companion; APPNAME="Knovent Companion"; APP="$BUILD/dist/$APPNAME.app"
ZIP="dist/Knovent-Companion-$VERSION-macOS-$TAG.zip"

# KNOVENT_CHANNEL=development allows http://localhost-style addresses for a developer's own build; the default (production) accepts exact https host names only, and says why when it refuses
PYTHONPATH=src python3 - "$@" <<'PY'
import json, os, sys, pathlib
from faqengine.companion.origins import validate_origin
dev = os.environ.get("KNOVENT_CHANNEL") == "development"
try:
    origins = [validate_origin(o, development=dev) for o in sys.argv[1:]]
except ValueError as e:
    sys.exit(f"Refused: {e}")
pathlib.Path("src/faqengine/companion/build_config.json").write_text(json.dumps({"schema": 1, "channel": "development" if dev else "production", "origins": origins}))
PY
trap 'rm -f src/faqengine/companion/build_config.json' EXIT
rm -rf "$BUILD"; mkdir -p "$BUILD" dist
python3 -m venv "$BUILD/venv" && "$BUILD/venv/bin/pip" install -q pyinstaller
# onedir + windowed is what PyInstaller supports for a macOS .app (onefile + windowed is deprecated)
"$BUILD/venv/bin/pyinstaller" --noconfirm --clean --onedir --windowed --name "$APPNAME" \
  --osx-bundle-identifier ai.knovent.companion.app --target-architecture "$ARCH" \
  --paths src --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  --add-data "$PWD/src/faqengine/companion/catalogue.json:faqengine/companion" \
  --add-data "$PWD/src/faqengine/companion/build_config.json:faqengine/companion" \
  scripts/companion_entry.py

# version and bundle metadata
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist" 2>/dev/null || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP/Contents/Info.plist" 2>/dev/null || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$APP/Contents/Info.plist" 2>/dev/null || true
find "$APP" \( -name ".DS_Store" -o -name "._*" \) -delete
xattr -cr "$APP" 2>/dev/null || true

# signing, inside out: every Mach-O file first, the bundle last (never --deep: it hides what was signed)
SIGNED_WITH="ad-hoc (development build)"
sign() { # sign <path>
  if [ -n "${KNOVENT_SIGN_IDENTITY:-}" ]; then
    codesign --force --timestamp --options runtime --entitlements scripts/macos/entitlements.plist --sign "$KNOVENT_SIGN_IDENTITY" "$1"
  else
    codesign --force --sign - "$1"
  fi
}
[ -n "${KNOVENT_SIGN_IDENTITY:-}" ] && SIGNED_WITH="Developer ID: $KNOVENT_SIGN_IDENTITY"
find "$APP" -type f -print0 | while IFS= read -r -d '' f; do
  case "$(file -b "$f")" in *Mach-O*) sign "$f";; esac
done
sign "$APP"

make_zip() { rm -f "$ZIP"; ditto -c -k --keepParent --norsrc --noextattr --noqtn "$APP" "$ZIP"; }  # --norsrc/--noextattr: no AppleDouble "._" files
make_zip

NOTARIZED="NO (not requested: set KNOVENT_SIGN_IDENTITY and KNOVENT_NOTARY_PROFILE)"
if [ -n "${KNOVENT_SIGN_IDENTITY:-}" ] && [ -n "${KNOVENT_NOTARY_PROFILE:-}" ]; then
  OUT="$(xcrun notarytool submit "$ZIP" --keychain-profile "$KNOVENT_NOTARY_PROFILE" --wait 2>&1)"; echo "$OUT"
  if echo "$OUT" | grep -q "status: Accepted"; then
    xcrun stapler staple "$APP" && xcrun stapler validate "$APP" && make_zip && NOTARIZED="YES (Apple accepted it; ticket stapled)"
  else
    NOTARIZED="NO (Apple did not accept it: see the output above; 'xcrun notarytool log <id> --keychain-profile ...' says why)"
  fi
elif [ -n "${KNOVENT_NOTARY_PROFILE:-}" ]; then
  NOTARIZED="NO (a notary profile was given but no signing identity)"
fi
( cd dist && shasum -a 256 "$(basename "$ZIP")" > "$(basename "$ZIP").sha256" )
echo ""; echo "built:      $ZIP"; echo "version:    $VERSION  ($TAG)"; echo "signed with: $SIGNED_WITH"; echo "notarized:  $NOTARIZED"; echo "checksum:   dist/$(basename "$ZIP").sha256"
echo "next:       scripts/verify_companion_macos.sh \"$APP\" [--release]"
