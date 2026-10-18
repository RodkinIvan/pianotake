#!/usr/bin/env bash
# Build pianotake.app and a DMG. Signing and notarization switch on via environment:
#
#   PIANOTAKE_SIGN_ID         "Developer ID Application: Your Name (TEAMID)"   (default: ad-hoc)
#   PIANOTAKE_NOTARY_PROFILE  keychain profile made with `xcrun notarytool store-credentials`
#     or, for CI:  NOTARY_KEY_PATH + NOTARY_KEY_ID + NOTARY_ISSUER  (App Store Connect API key)
#   PYTHON                    Python with pyinstaller + pianotake's dependencies (default: python3)
#
# Output: dist/pianotake.app and dist/pianotake-<version>-macos-<arch>.dmg
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python3}"
SIGN_ID="${PIANOTAKE_SIGN_ID:--}"
DIST="$ROOT/dist"
WORK="$ROOT/build/macos"
VERSION="$("$PY" -c "import re,sys; print(re.search(r'__version__ = \"([^\"]+)\"', open(sys.argv[1]).read()).group(1))" "$ROOT/pianotake/__init__.py")"
ARCH="$(uname -m)"
APP="$DIST/pianotake.app"
DMG="$DIST/pianotake-$VERSION-macos-$ARCH.dmg"

notarize_enabled() { [[ -n "${PIANOTAKE_NOTARY_PROFILE:-}" || -n "${NOTARY_KEY_PATH:-}" ]]; }
if notarize_enabled && [[ "$SIGN_ID" == "-" ]]; then
  echo "Notarization needs a Developer ID signature: set PIANOTAKE_SIGN_ID." >&2; exit 1
fi

sign() {  # sign <path> [extra codesign args...]; quiet unless it fails
  local target="$1"; shift
  local args=(--force --sign -)
  [[ "$SIGN_ID" != "-" ]] && args=(--force --timestamp --options runtime --sign "$SIGN_ID")
  local out
  if ! out="$(codesign "${args[@]}" "$@" "$target" 2>&1)"; then
    echo "$out" >&2
    return 1
  fi
}

notarize() {  # notarize <file>
  echo "==> Notarizing $(basename "$1") (this usually takes a few minutes)"
  if [[ -n "${PIANOTAKE_NOTARY_PROFILE:-}" ]]; then
    xcrun notarytool submit "$1" --keychain-profile "$PIANOTAKE_NOTARY_PROFILE" --wait
  else
    xcrun notarytool submit "$1" --key "$NOTARY_KEY_PATH" --key-id "$NOTARY_KEY_ID" \
      --issuer "$NOTARY_ISSUER" --wait
  fi
}

echo "==> pianotake $VERSION ($ARCH), signing: ${SIGN_ID}"
rm -rf "$WORK" "$APP" "$DMG"
mkdir -p "$WORK" "$DIST"

echo "==> Icon"
"$PY" "$HERE/make_icon.py" "$WORK/icon" >/dev/null

echo "==> PyInstaller"
PIANOTAKE_ICON="$WORK/icon/pianotake.icns" "$PY" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST" --workpath "$WORK/pyinstaller" "$HERE/pianotake.spec"
rm -rf "$DIST/pianotake"            # the intermediate onedir folder; the .app is the product

echo "==> Signing the app (inside out)"
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then sign "$f"; fi
done < <(find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) \
         ! -path "$APP/Contents/MacOS/pianotake" -print0)
sign "$APP" --entitlements "$HERE/entitlements.plist"
codesign --verify --strict --deep "$APP"

if notarize_enabled; then
  ditto -c -k --keepParent "$APP" "$WORK/pianotake.zip"
  notarize "$WORK/pianotake.zip"
  xcrun stapler staple "$APP"        # the app works offline after being copied out of the DMG
fi

echo "==> DMG"
STAGE="$WORK/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "pianotake $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
if [[ "$SIGN_ID" != "-" ]]; then
  codesign --force --timestamp --sign "$SIGN_ID" "$DMG"
fi
if notarize_enabled; then
  notarize "$DMG"
  xcrun stapler staple "$DMG"
  spctl --assess --type open --context context:primary-signature -vv "$DMG"
  spctl --assess --type execute -vv "$APP"
fi

echo "==> Done"
echo "    $APP"
echo "    $DMG ($(du -h "$DMG" | cut -f1))"
[[ "$SIGN_ID" == "-" ]] && echo "    (ad-hoc signed: other Macs will show an 'unidentified developer' warning)"
exit 0
