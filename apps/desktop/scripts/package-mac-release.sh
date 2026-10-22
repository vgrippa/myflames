#!/bin/sh
# Build the Developer ID signed, notarized Apple Silicon DMG for a release.
#
#   MYFLAMES_MAC_IDENTITY   the Developer ID Application certificate: its name
#                           without the "Developer ID Application: " prefix, or
#                           its SHA-1 hash (security find-identity -v -p codesigning)
#   APPLE_KEYCHAIN_PROFILE  a notarytool profile (xcrun notarytool store-credentials)
#
# Build the workspace and the backend first (see README.md). Local builds stay
# unsigned: electron-builder.json keeps identity null and notarize false, and
# only this script overrides them.
set -eu

die() { echo "package-mac-release: $*" >&2; exit 2; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "build on an Apple Silicon Mac"
[ -n "${MYFLAMES_MAC_IDENTITY:-}" ] || die "set MYFLAMES_MAC_IDENTITY to the Developer ID Application certificate's name or SHA-1 hash"
[ -n "${APPLE_KEYCHAIN_PROFILE:-}" ] || die "set APPLE_KEYCHAIN_PROFILE to a profile saved with xcrun notarytool store-credentials"
case "$MYFLAMES_MAC_IDENTITY" in
  "Developer ID Application:"*) die "leave the \"Developer ID Application: \" prefix out of MYFLAMES_MAC_IDENTITY" ;;
esac
# Only the identity named above signs; no certificate file or other name may.
unset CSC_LINK CSC_KEY_PASSWORD CSC_NAME CSC_IDENTITY_AUTO_DISCOVERY
# macOS will not hand out the notary profile while the screen is locked, so keep
# the display awake until the build ends (a manual lock still stops it).
if [ -z "${MYFLAMES_RELEASE_AWAKE:-}" ]; then
  MYFLAMES_RELEASE_AWAKE=1 exec caffeinate -dim sh "$0" "$@"
fi
locked_hint="if notarytool found no Keychain item for the profile, the screen was probably locked: unlock the Mac and run this again"

cd "$(dirname "$0")/.."
[ -x build/backend/myflames-backend/myflames-backend ] || die "build the backend first (scripts/build-backend.py)"

matches=$(security find-identity -v -p codesigning | grep '"Developer ID Application: ' | grep -F -- "$MYFLAMES_MAC_IDENTITY" || true)
count=$(printf '%s\n' "$matches" | grep -c . || true)
[ "$count" -eq 1 ] || die "MYFLAMES_MAC_IDENTITY matches $count Developer ID Application identities; use the SHA-1 hash"
identity=$(printf '%s\n' "$matches" | awk '{print $2}')
xcrun notarytool history --keychain-profile "$APPLE_KEYCHAIN_PROFILE" >/dev/null \
  || die "notarytool profile \"$APPLE_KEYCHAIN_PROFILE\" could not sign in; $locked_hint"

version=$(node -p 'require("./package.json").version')
app=dist/mac-arm64/myflames.app
name=myflames-$version-macos-arm64.dmg
dmg=dist/$name
rm -rf "$app" "$dmg" "$dmg.sha256"

# Signs every binary with the hardened runtime, notarizes and staples the app,
# then builds the DMG around it.
npx --no-install electron-builder --mac dmg --arm64 --config electron-builder.json --publish never \
  -c.mac.identity="$identity" -c.mac.notarize=true \
  || die "electron-builder failed (output above); $locked_hint"

# electron-builder hands the signer the certificate's name, not its hash, and
# the signer takes the first certificate with that name; refuse another one.
certs=$(mktemp -d)
for signed in "$app" "$app/Contents/Resources/backend/myflames-backend"; do
  rm -f "$certs"/c*
  codesign -d --extract-certificates="$certs/c" "$signed" 2>/dev/null
  used=$(shasum -a 1 "$certs/c0" | awk '{print toupper($1)}')
  [ "$used" = "$identity" ] || die "$signed was signed with certificate $used, not $identity: another Developer ID Application certificate has the same name; remove the one you do not use from the keychain"
done
rm -rf "$certs"

node scripts/smoke.cjs "$app"

codesign --force --sign "$identity" --timestamp "$dmg"
result=$(xcrun notarytool submit "$dmg" --keychain-profile "$APPLE_KEYCHAIN_PROFILE" --wait --output-format json) \
  || die "notarytool submit for $name failed (output above); $locked_hint"
status=$(printf '%s' "$result" | node -e 'let s = ""; process.stdin.on("data", (d) => { s += d; }).on("end", () => console.log(JSON.parse(s).status || ""))')
if [ "$status" != Accepted ]; then
  echo "$result" >&2
  die "notarization of $name ended with \"$status\"; xcrun notarytool log <id> --keychain-profile $APPLE_KEYCHAIN_PROFILE names the files"
fi
xcrun stapler staple "$dmg"

notarized() {
  spctl --assess -vv "$@" 2>&1 | grep -q 'source=Notarized Developer ID' || die "Gatekeeper does not accept $*"
}
codesign --verify --deep --strict "$app"
notarized --type execute "$app"
notarized --type open --context context:primary-signature "$dmg"
xcrun stapler validate -q "$dmg"
mount=$(mktemp -d)
hdiutil attach -nobrowse -readonly -mountpoint "$mount" "$dmg" >/dev/null
trap 'hdiutil detach -quiet "$mount" 2>/dev/null || true' EXIT
notarized --type execute "$mount/myflames.app"
xcrun stapler validate -q "$mount/myflames.app"
hdiutil detach -quiet "$mount"
trap - EXIT

# The in-app update download stops at 150 MiB (myflames/app_update.py).
size=$(stat -f %z "$dmg")
[ "$size" -le $((150 * 1024 * 1024)) ] || die "$name is $size bytes, over the 150 MiB update download limit"
(cd dist && shasum -a 256 "$name" > "$name.sha256")
echo "$dmg"
