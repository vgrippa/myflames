#!/bin/sh
# Build the Developer ID signed, notarized Apple Silicon DMG for a release.
#
#   MYFLAMES_MAC_IDENTITY   the Developer ID Application certificate: its name
#                           without the "Developer ID Application: " prefix, or
#                           its SHA-1 hash (security find-identity -v -p codesigning)
#   and one way to sign in to the notary service:
#   APPLE_KEYCHAIN_PROFILE  a notarytool profile (xcrun notarytool store-credentials),
#                           which macOS will not hand out while the screen is locked
#   APPLE_API_KEY, APPLE_API_KEY_ID, APPLE_API_ISSUER
#                           an App Store Connect API key file (AuthKey_<id>.p8),
#                           its key ID and issuer ID; read from disk, so a locked
#                           screen does not stop the build
#
# Build the workspace and the backend first (see README.md). Local builds stay
# unsigned: electron-builder.json keeps identity null and notarize false, and
# only this script overrides them.
set -eu

die() { echo "package-mac-release: $*" >&2; exit 2; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "build on an Apple Silicon Mac"
[ -n "${MYFLAMES_MAC_IDENTITY:-}" ] || die "set MYFLAMES_MAC_IDENTITY to the Developer ID Application certificate's name or SHA-1 hash"
case "$MYFLAMES_MAC_IDENTITY" in
  "Developer ID Application:"*) die "leave the \"Developer ID Application: \" prefix out of MYFLAMES_MAC_IDENTITY" ;;
esac
# electron-builder signs in with the first of an Apple ID, an API key or a
# profile it finds, so leave it exactly one.
unset APPLE_ID APPLE_APP_SPECIFIC_PASSWORD APPLE_TEAM_ID APPLE_KEYCHAIN
if [ -n "${APPLE_API_KEY:-}" ]; then
  [ -n "${APPLE_API_KEY_ID:-}" ] && [ -n "${APPLE_API_ISSUER:-}" ] || die "set APPLE_API_KEY_ID and APPLE_API_ISSUER with APPLE_API_KEY"
  case "$APPLE_API_KEY" in /*) ;; *) APPLE_API_KEY=$PWD/$APPLE_API_KEY ;; esac
  [ -r "$APPLE_API_KEY" ] || die "cannot read the API key file $APPLE_API_KEY"
  export APPLE_API_KEY
  unset APPLE_KEYCHAIN_PROFILE
  notary_name="API key $APPLE_API_KEY_ID"
  locked_hint="check the key file, key ID and issuer ID"
  notary() { xcrun notarytool "$@" --key "$APPLE_API_KEY" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER"; }
elif [ -n "${APPLE_KEYCHAIN_PROFILE:-}" ]; then
  # electron-builder refuses a partial API key before it looks at a profile.
  unset APPLE_API_KEY APPLE_API_KEY_ID APPLE_API_ISSUER
  notary_name="profile \"$APPLE_KEYCHAIN_PROFILE\""
  locked_hint="if notarytool found no Keychain item for the profile, the screen was probably locked: unlock the Mac and run this again, or use an API key"
  notary() { xcrun notarytool "$@" --keychain-profile "$APPLE_KEYCHAIN_PROFILE"; }
else
  die "set APPLE_KEYCHAIN_PROFILE, or APPLE_API_KEY with APPLE_API_KEY_ID and APPLE_API_ISSUER"
fi
# Only the identity named above signs; no certificate file or other name may.
unset CSC_LINK CSC_KEY_PASSWORD CSC_NAME CSC_IDENTITY_AUTO_DISCOVERY
# Keep the display from sleeping (and locking) until the build ends; a manual
# lock still closes the keychain.
if [ -z "${MYFLAMES_RELEASE_AWAKE:-}" ]; then
  MYFLAMES_RELEASE_AWAKE=1 exec caffeinate -dim sh "$0" "$@"
fi

cd "$(dirname "$0")/.."
[ -x build/backend/myflames-backend/myflames-backend ] || die "build the backend first (scripts/build-backend.py)"

matches=$(security find-identity -v -p codesigning | grep '"Developer ID Application: ' | grep -F -- "$MYFLAMES_MAC_IDENTITY" || true)
count=$(printf '%s\n' "$matches" | grep -c . || true)
[ "$count" -eq 1 ] || die "MYFLAMES_MAC_IDENTITY matches $count Developer ID Application identities; use the SHA-1 hash"
identity=$(printf '%s\n' "$matches" | awk '{print $2}')
notary history >/dev/null || die "notarytool could not sign in with the $notary_name; $locked_hint"

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
result=$(notary submit "$dmg" --wait --output-format json) \
  || die "notarytool submit for $name failed (output above); $locked_hint"
status=$(printf '%s' "$result" | node -e 'let s = ""; process.stdin.on("data", (d) => { s += d; }).on("end", () => console.log(JSON.parse(s).status || ""))')
if [ "$status" != Accepted ]; then
  echo "$result" >&2
  die "notarization of $name ended with \"$status\"; xcrun notarytool log <id> with the $notary_name names the files"
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
