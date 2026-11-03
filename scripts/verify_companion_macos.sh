#!/bin/zsh
# Checks a built "Knovent Companion.app" and its ZIP. Every line is PASS, FAIL or NOT RUN with the reason; nothing is assumed.
#   scripts/verify_companion_macos.sh "build/companion/dist/Knovent Companion.app" [--release]
# --release additionally REQUIRES a Developer ID signature with the hardened runtime, Gatekeeper acceptance and a stapled notarization ticket.
APP="${1:?path to the .app}"; REL="${2:-}"; FAILS=0
ok() { printf "PASS     %s\n" "$1"; }; bad() { printf "FAIL     %s\n" "$1"; FAILS=$((FAILS+1)); }; skip() { printf "NOT RUN  %s\n" "$1"; }
EXE="$APP/Contents/MacOS/$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Contents/Info.plist" 2>/dev/null)"
# 1. bundle
[ -d "$APP/Contents" ] && [ -x "$EXE" ] && ok "bundle has Contents/MacOS executable ($(basename "$EXE"))" || bad "bundle structure"
BID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist" 2>/dev/null)"; [ "$BID" = "ai.knovent.companion.app" ] && ok "bundle identifier $BID" || bad "bundle identifier is '$BID'"
V="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null)"; [ -n "$V" ] && ok "version $V" || bad "no version"
[ -d "$APP/Contents/Frameworks" ] || [ -d "$APP/Contents/Resources" ] && ok "onedir layout (libraries inside the bundle, not unpacked at run time)" || bad "not an onedir bundle"
# 2. architecture
ARCHS=$(find "$APP" -type f -print0 | while IFS= read -r -d '' f; do case "$(file -b "$f")" in *Mach-O*) lipo -archs "$f" 2>/dev/null;; esac; done | tr ' ' '\n' | sort | uniq -c | tr -s ' ' | tr '\n' ';')
N=$(find "$APP" -type f -print0 | while IFS= read -r -d '' f; do case "$(file -b "$f")" in *Mach-O*) echo x;; esac; done | wc -l | tr -d ' ')
echo "$ARCHS" | grep -qE "^ ?[0-9]+ (arm64|x86_64);$" && ok "all $N Mach-O files are one architecture:$ARCHS" || bad "mixed architectures:$ARCHS"
# 3. embedded dependencies: nothing may point outside the bundle or the system
STRAY=$(find "$APP" -type f -print0 | while IFS= read -r -d '' f; do case "$(file -b "$f")" in *Mach-O*) otool -L "$f" 2>/dev/null | tail -n +2 | awk '{print $1}' | grep -vE '^(@rpath|@loader_path|@executable_path|/usr/lib/|/System/)' ;; esac; done | sort -u)
[ -z "$STRAY" ] && ok "no library is loaded from outside the bundle or the system" || bad "libraries outside the bundle: $(echo $STRAY | tr '\n' ' ')"
BUILDPATH=$(grep -rlE "/Users/[a-z]+/|/opt/anaconda|/opt/homebrew" "$APP/Contents/MacOS" "$APP/Contents/Info.plist" 2>/dev/null | head -3)
[ -z "$BUILDPATH" ] && ok "no build-machine path in the launcher or Info.plist" || bad "build-machine path in: $BUILDPATH"
# 4. signature
codesign --verify --deep --strict --verbose=1 "$APP" >/dev/null 2>&1 && ok "signature is valid and covers every file (codesign --verify --deep --strict)" || bad "signature does not verify"
INFO="$(codesign -dvvv "$APP" 2>&1)"
if echo "$INFO" | grep -q "Signature=adhoc"; then
  [ -n "$REL" ] && bad "ad-hoc signature: a release needs a Developer ID Application signature (requires an Apple Developer account)" || skip "Developer ID signature: this is an ad-hoc development build (requires an Apple Developer account)"
else
  echo "$INFO" | grep -q "Authority=Developer ID Application" && ok "signed by a Developer ID Application identity: $(echo "$INFO" | grep -m1 'Authority=Developer ID Application' | cut -d= -f2)" || bad "not a Developer ID Application signature"
  TEAM=$(echo "$INFO" | grep -m1 TeamIdentifier | cut -d= -f2); [ -n "$TEAM" ] && [ "$TEAM" != "not set" ] && ok "Team ID $TEAM" || bad "no Team ID"
  echo "$INFO" | grep -q "flags=.*runtime" && ok "hardened runtime is on" || bad "hardened runtime is off (notarization requires it)"
  echo "$INFO" | grep -q "Timestamp=" && ok "secure timestamp present" || bad "no secure timestamp (notarization requires it)"
fi
# 5. Gatekeeper and notarization
SP="$(spctl --assess --type execute -vv "$APP" 2>&1)"
if echo "$SP" | grep -q "accepted"; then ok "Gatekeeper accepts it ($(echo "$SP" | grep -m1 source | tr -s ' '))"; else [ -n "$REL" ] && bad "Gatekeeper rejects it: $(echo "$SP" | head -1)" || skip "Gatekeeper acceptance: rejected as expected for an un-notarized build ($(echo "$SP" | head -1 | cut -c1-80))"; fi
xcrun stapler validate "$APP" >/dev/null 2>&1 && ok "notarization ticket is stapled and valid" || { [ -n "$REL" ] && bad "no valid stapled notarization ticket" || skip "notarization ticket: none (requires an Apple Developer account)"; }
# 6. the ZIP
Z=$(ls -t dist/Knovent-Companion-*-macOS-*.zip 2>/dev/null | head -1)
if [ -n "$Z" ]; then
  BAD=$(unzip -l "$Z" | grep -E "(^|/)\._|__MACOSX|\.DS_Store" | wc -l | tr -d ' '); [ "$BAD" = "0" ] && ok "$(basename "$Z") has no AppleDouble, __MACOSX or .DS_Store entries" || bad "$(basename "$Z") has $BAD metadata entries"
  [ -f "$Z.sha256" ] && (cd dist && shasum -a 256 -c "$(basename "$Z").sha256" >/dev/null 2>&1) && ok "checksum file matches the ZIP" || bad "checksum missing or wrong"
else skip "ZIP checks: no ZIP in dist/"; fi
echo ""; [ $FAILS -eq 0 ] && echo "RESULT: no failures" || echo "RESULT: $FAILS failure(s)"; exit $FAILS
