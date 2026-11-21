#!/bin/sh
# One signed-release entry point. Build the untracked workspace output before
# the backend. Usage: npm run release:mac -- <full-HEAD-SHA> [--resume]
set -eu

die() { echo "release-mac: $*" >&2; exit 2; }
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "build on an Apple Silicon Mac"
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || die "usage: release-mac.sh <full-HEAD-SHA> [--resume]"
commit=$1
resume=
if [ "$#" -eq 2 ]; then
  [ "$2" = --resume ] || die "usage: release-mac.sh <full-HEAD-SHA> [--resume]"
  resume=--resume
fi

cd "$(dirname "$0")/.."
desktop=$PWD
root=$(cd ../.. && pwd)
build_python=${MYFLAMES_DESKTOP_BUILD_PYTHON:-python3.14}
export MYFLAMES_DESKTOP_BUILD_VENV="${MYFLAMES_DESKTOP_BUILD_VENV:-$root/local/desktop-build-venv}"
export MYFLAMES_MYSQL_CLIENT_CACHE="${MYFLAMES_MYSQL_CLIENT_CACHE:-$HOME/.cache/myflames/mysql-client-cache}"
# Normalize overrides before a child changes directory. Build outputs remain
# worktree-local; only verified client archives use the shared cache.
case "$MYFLAMES_DESKTOP_BUILD_VENV" in /*) ;; *) MYFLAMES_DESKTOP_BUILD_VENV=$desktop/$MYFLAMES_DESKTOP_BUILD_VENV ;; esac
case "$MYFLAMES_MYSQL_CLIENT_CACHE" in /*) ;; *) MYFLAMES_MYSQL_CLIENT_CACHE=$desktop/$MYFLAMES_MYSQL_CLIENT_CACHE ;; esac
command -v python3 >/dev/null 2>&1 || die "python3 is required for release preflight"
python3 scripts/release_state.py preflight --root "$root" --commit "$commit" \
  --python "$build_python" --venv "$MYFLAMES_DESKTOP_BUILD_VENV" --cache "$MYFLAMES_MYSQL_CLIENT_CACHE"
# This mode shares all credential and identity checks with the packager. It
# neither requires an existing backend nor signs or builds anything.
sh scripts/package-mac-release.sh --preflight

if [ -z "${MYFLAMES_RELEASE_AWAKE:-}" ]; then
  MYFLAMES_RELEASE_AWAKE=1 exec caffeinate -dim sh "$desktop/scripts/release-mac.sh" "$@"
fi

# Install both lockfiles on every release, including resume. The settings
# smokes attach to Electron, so workspace dependencies need no browser download.
python3 scripts/release_state.py run --label 'installing desktop build dependencies' -- npm ci
PUPPETEER_SKIP_DOWNLOAD=true python3 scripts/release_state.py run --label 'installing workspace dependencies' -- npm --prefix "$root/apps/workspace" ci
NODE_ENV=production python3 scripts/release_state.py run --label 'building the workspace' -- npm --prefix "$root/apps/workspace" run build

# A valid backend is reusable only for an explicit resume. A normal invocation
# always provisions and builds before the packager's clean start.
if [ "$resume" = --resume ] && python3 scripts/release_state.py manifest-check --root "$root"; then
  echo "release-mac: resuming with the verified backend build manifest"
else
  "$build_python" scripts/release_state.py venv --root "$root" --venv "$MYFLAMES_DESKTOP_BUILD_VENV"
  python3 scripts/release_state.py run --label 'building the backend' -- \
    "$MYFLAMES_DESKTOP_BUILD_VENV/bin/python" scripts/build-backend.py
fi
# Recheck the exact commit and cleanliness after provisioning/building, too.
# Neither the workspace source nor its freshly built output may change mid-build.
[ "$(git -C "$root" rev-parse HEAD)" = "$commit" ] || die "HEAD changed during the build"
[ -z "$(git -C "$root" status --porcelain)" ] || die "git tree changed during the build"
# Ignored output is invisible to git status; compare the backend's recorded
# digest and the source stamp before handing it to the packager.
python3 scripts/release_state.py manifest-check --root "$root"
if [ "$resume" = --resume ]; then
  exec sh scripts/package-mac-release.sh --resume
fi
exec sh scripts/package-mac-release.sh
