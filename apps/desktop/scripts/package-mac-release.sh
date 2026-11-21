#!/bin/sh
# Build the Developer ID signed, notarized Apple Silicon DMG for a release, and
# the zip of the same app that the in-app update installs (Squirrel.Mac).
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
# Use release-mac.sh to build the workspace, then the backend and prerequisites. Local builds stay
# unsigned: electron-builder.json keeps identity null and notarize false, and
# only this script overrides them.
set -eu

die() { echo "package-mac-release: $*" >&2; exit 2; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "build on an Apple Silicon Mac"
mode=${1:-}
[ "$#" -le 1 ] || die "usage: package-mac-release.sh [--resume|--preflight]"
case "$mode" in ''|--resume|--preflight) ;; *) die "usage: package-mac-release.sh [--resume|--preflight]" ;; esac
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
  [ -r "$APPLE_API_KEY" ] || die "cannot read the API key file"
  export APPLE_API_KEY
  unset APPLE_KEYCHAIN_PROFILE
  notary_name="API key"
  locked_hint="check the key file, key ID and issuer ID"
elif [ -n "${APPLE_KEYCHAIN_PROFILE:-}" ]; then
  # electron-builder refuses a partial API key before it looks at a profile.
  unset APPLE_API_KEY APPLE_API_KEY_ID APPLE_API_ISSUER
  notary_name="Keychain profile"
  locked_hint="if notarytool found no Keychain item for the profile, the screen was probably locked: unlock the Mac and run this again, or use an API key"
else
  die "set APPLE_KEYCHAIN_PROFILE, or APPLE_API_KEY with APPLE_API_KEY_ID and APPLE_API_ISSUER"
fi
# Only the identity named above signs; no certificate file or other name may.
unset CSC_LINK CSC_KEY_PASSWORD CSC_NAME CSC_IDENTITY_AUTO_DISCOVERY
cd "$(dirname "$0")/.."
desktop=$PWD
state() { python3 scripts/release_state.py "$@"; }
notary() { state notary -- "$@"; }
run() { label=$1; shift; state run --label "$label" -- "$@"; }

if [ "$mode" != --preflight ]; then
  [ -x build/backend/myflames-backend/myflames-backend ] || die "rebuild the backend with release-mac.sh"
  # Includes the source stamp and digest of the freshly built workspace.
  # Never rebuild it after bundling: that could validate different output.
  state manifest-check
fi

matches=$(security find-identity -v -p codesigning | grep '"Developer ID Application: ' | grep -F -- "$MYFLAMES_MAC_IDENTITY" || true)
count=$(printf '%s\n' "$matches" | grep -c . || true)
[ "$count" -eq 1 ] || die "MYFLAMES_MAC_IDENTITY matches $count Developer ID Application identities; use the SHA-1 hash"
identity=$(printf '%s\n' "$matches" | awk '{print $2}')
# The team this build signs with, from the matched identity's name; spctl's
# notarization origin is checked against it, never a hard-coded team.
team_id=$(printf '%s\n' "$matches" | state team-id) || die "the matched identity's name does not end with a team ID"
notary history >/dev/null || die "notarytool could not sign in with the $notary_name; $locked_hint"
[ "$mode" != --preflight ] || exit 0

# Keep the display from sleeping (and locking) until the build ends; a manual
# lock still closes the keychain. --preflight never re-executes or builds.
if [ -z "${MYFLAMES_RELEASE_AWAKE:-}" ]; then
  MYFLAMES_RELEASE_AWAKE=1 exec caffeinate -dim sh "$desktop/scripts/package-mac-release.sh" "$@"
fi

# The bundled MySQL client: the archive pinned in mysql-client.lock.json,
# verified against its SHA-256 before anything is extracted.
version=$(node -p 'require("./package.json").version')
app=dist/mac-arm64/myflames.app
name=myflames-$version-macos-arm64.dmg
dmg=dist/$name
zipname=myflames-$version-macos-arm64.zip
zip=dist/$zipname
if [ "$mode" = --resume ]; then
  first=$(state prepare --identity "$identity" --resume)
else
  first=$(state prepare --identity "$identity")
fi
echo "package-mac-release: next stage $first"
needs() { [ "$(state next)" = "$1" ]; }

# Always reap smoke runners, and unmount/clean temporary verification folders,
# including when Apple or a check fails. Each runner forwards TERM to node and
# lets the smoke clean up its own app/backend process groups.
editor_pid='' preferences_pid='' certs='' mount='' unzipped=''
cleanup() {
  exit_status=$?
  trap - 0 INT TERM HUP
  set +e
  for pid in "$editor_pid" "$preferences_pid"; do
    [ -z "$pid" ] || kill -TERM "$pid" 2>/dev/null
  done
  for pid in "$editor_pid" "$preferences_pid"; do
    [ -z "$pid" ] || wait "$pid"
  done
  if [ -n "$mount" ]; then
    if hdiutil detach -quiet "$mount" 2>/dev/null; then rmdir "$mount"; fi
  fi
  for temporary in "$certs" "$unzipped"; do
    [ -z "$temporary" ] || rm -rf "$temporary"
  done
  exit "$exit_status"
}
trap cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM HUP

if needs app; then
  # The bundled MySQL client: the archive pinned in mysql-client.lock.json,
  # verified against its SHA-256 before anything is extracted.
  python3 scripts/fetch-mysql-client.py --platform darwin-arm64 || die "could not fetch the bundled MySQL client (output above)"
  rm -rf "$app" "$dmg" "$dmg.sha256" "$zip" "$zip.sha256" "$zip.blockmap"
  # Signs every binary with the hardened runtime, notarizes and staples the
  # app, then builds the DMG and the zip around it. This stage is indivisible:
  # a partial electron-builder run is rebuilt, never treated as signed output.
  run 'building, signing and notarizing the app' npx --no-install electron-builder --mac dmg zip --arm64 --config electron-builder.json --publish never \
    -c.mac.identity="$identity" -c.mac.notarize=true \
    || die "electron-builder failed (output above); $locked_hint"
  state complete app "$app" "$dmg" "$zip"
fi

if needs verify-app; then
  # electron-builder hands the signer the certificate's name, not its hash,
  # and the signer takes the first certificate with that name; refuse another.
  certs=$(mktemp -d)
  client=$app/Contents/Resources/mysql-client
  for signed in "$app" "$app/Contents/Resources/backend/myflames-backend" \
    "$client/bin/mysql" "$client/bin/mysqldump" "$client"/lib/*.dylib; do
    rm -f "$certs"/c*
    codesign -d --extract-certificates="$certs/c" "$signed" 2>/dev/null
    used=$(shasum -a 1 "$certs/c0" | awk '{print toupper($1)}')
    [ "$used" = "$identity" ] || die "$signed was signed with certificate $used, not $identity: another Developer ID Application certificate has the same name; remove the one you do not use from the keychain"
  done
  rm -rf "$certs"
  certs=
  # Re-signed with the hardened runtime, the client must still load its own
  # libraries (library validation): run it with nothing else on PATH.
  env -i PATH=/usr/bin:/bin "$client/bin/mysql" --version >/dev/null || die "the signed bundled mysql does not start"
  env -i PATH=/usr/bin:/bin "$client/bin/mysqldump" --version >/dev/null || die "the signed bundled mysqldump does not start"
  run 'the packaged smoke' node scripts/smoke.cjs "$app"
  state complete verify-app "$app"
fi

if [ "$(state next)" != 'done' ]; then
  # Separate temporary profiles in the smokes make these safe to run together
  # against the same signed bundle while Apple handles the DMG.
  if state smoke-needed editor-settings; then
    python3 scripts/release_state.py run --label 'the editor-settings smoke' -- node scripts/editor-settings-smoke.cjs "$app" &
    editor_pid=$!
  fi
  if state smoke-needed preferences; then
    python3 scripts/release_state.py run --label 'the preferences smoke' -- node scripts/preferences-smoke.cjs "$app" &
    preferences_pid=$!
  fi
fi

if needs dmg-notarize; then
  if ! state pending dmg-notarize; then
    codesign --force --sign "$identity" --timestamp "$dmg"
    state checkpoint dmg-notarize "$dmg"
  fi
  # Submit without --wait, persist its ID, then wait/info on that same ID.
  # Only transport failures retry; Apple's exact error precedes the fix.
  state notarize-dmg "$dmg" || die "notarytool submit for $name failed (output above); $locked_hint"
  state complete dmg-notarize "$dmg"
fi

if needs staple; then
  xcrun stapler staple "$dmg"
  state checkpoint staple "$dmg"
  state complete staple "$dmg"
fi

smoke_failed=0
if [ -n "$editor_pid" ]; then
  if wait "$editor_pid"; then state smoke-complete editor-settings; else smoke_failed=1; fi
  editor_pid=
fi
if [ -n "$preferences_pid" ]; then
  if wait "$preferences_pid"; then state smoke-complete preferences; else smoke_failed=1; fi
  preferences_pid=
fi
[ "$smoke_failed" -eq 0 ] || die "a packaged settings smoke failed (output above)"

notarized() {
  assessment=$(spctl --assess -vv "$@" 2>&1) || die "Gatekeeper does not accept $*"
  printf '%s\n' "$assessment" | grep -q 'source=Notarized Developer ID' || die "Gatekeeper does not accept $*"
  # spctl accepts any team's notarized Developer ID; the origin must be the
  # team this build signed with.
  printf '%s\n' "$assessment" | state notarized-origin "$team_id" || die "the notarization origin of $* is missing or names another team"
}
if needs verify; then
codesign --verify --deep --strict "$app"
notarized --type execute "$app"
notarized --type open --context context:primary-signature "$dmg"
xcrun stapler validate -q "$dmg"
mount=$(mktemp -d)
hdiutil attach -nobrowse -readonly -mountpoint "$mount" "$dmg" >/dev/null
notarized --type execute "$mount/myflames.app"
xcrun stapler validate -q "$mount/myflames.app"
hdiutil detach -quiet "$mount"
rmdir "$mount"
mount=
# The update zip: the same signed, stapled app, which Squirrel.Mac installs
# only when its signature meets the installed app's designated requirement.
unzipped=$(mktemp -d)
ditto -x -k "$zip" "$unzipped"
[ -d "$unzipped/myflames.app" ] || die "$zipname does not hold myflames.app"
codesign --verify --deep --strict "$unzipped/myflames.app"
notarized --type execute "$unzipped/myflames.app"
xcrun stapler validate -q "$unzipped/myflames.app"
rm -rf "$unzipped" "$zip.blockmap"
unzipped=
state complete verify "$app" "$dmg" "$zip"
fi

# The in-app download and update stop at 200 MiB (myflames/app_update.py);
# 3.3.3 and older stop their Download at 150 MiB.
if needs size; then
for file in "$dmg" "$zip"; do
  size=$(stat -f %z "$file")
  [ "$size" -le $((200 * 1024 * 1024)) ] || die "$file is $size bytes, over the 200 MiB update download limit"
  [ "$size" -le $((150 * 1024 * 1024)) ] || echo "package-mac-release: $file is over 150 MiB: 3.3.3 and older cannot download it in the app (they open the release page instead)" >&2
done
state complete size "$dmg" "$zip"
fi
# The app checks the update zip (and Download checks the DMG) against these.
if needs checksums; then
(cd dist && shasum -a 256 "$name" > "$name.sha256" && shasum -a 256 "$zipname" > "$zipname.sha256")
state complete checksums "$dmg.sha256" "$zip.sha256"
fi
# Refuse changes to sources or ignored output during signing and verification.
state manifest-check
echo "$dmg"
echo "$zip"
