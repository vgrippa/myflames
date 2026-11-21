#!/usr/bin/env bash
# Run workspace suites on Linux, mirroring the CI runners: ubuntu:24.04,
# Node 22, the Chrome for Testing headless shell the workspace's Puppeteer
# version pins, and the fonts the GitHub ubuntu-24.04 image ships that the
# suites render with (fonts-dejavu-core, fonts-liberation). The repository is
# mounted read-only and copied inside the container, so the host's
# node_modules (built for another platform) never runs; npm ci, npm run build
# and the suites all run on Linux. npm and Puppeteer downloads persist in a
# named Docker volume. Exit status is the suites'.
#
# usage: scripts/linux-suites.sh <suite.mjs>[,<suite.mjs>]
set -euo pipefail

suites="${1:-}"
if [ -z "$suites" ]; then
  echo "usage: scripts/linux-suites.sh <suite.mjs>[,<suite.mjs>]" >&2
  exit 2
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
image="myflames-linux-suites:ubuntu-24.04"
volume="myflames-linux-suites-cache"

# A read-only ~/.docker (some sandboxes) keeps buildx from recording builder
# activity; give Docker a writable config directory then.
if ! touch "${DOCKER_CONFIG:-$HOME/.docker}/buildx/activity/.probe" 2>/dev/null; then
  export DOCKER_CONFIG="${TMPDIR:-/tmp}/myflames-linux-suites-docker"
  mkdir -p "$DOCKER_CONFIG"
fi

if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker build --platform linux/amd64 --tag "$image" - <<'DOCKERFILE'
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils python3 \
      fonts-dejavu-core fonts-liberation \
      libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
      libcairo2 libcups2t64 libdrm2 libgbm1 libnspr4 libnss3 \
      libpango-1.0-0 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2 \
 && apt-get clean
# Node 22 (same major as the CI runners), latest v22.x release.
RUN tarball="$(curl -fsSL https://nodejs.org/dist/latest-v22.x/ | grep -oE 'node-v22[^"]*-linux-x64\.tar\.xz' | head -1)" \
 && curl -fsSL "https://nodejs.org/dist/latest-v22.x/${tarball}" | tar -xJ -C /usr/local --strip-components=1 \
 && node --version
DOCKERFILE
fi

if ! docker volume inspect "$volume" >/dev/null 2>&1; then
  docker volume create "$volume" >/dev/null
fi

exec docker run --init --interactive --platform linux/amd64 \
  --mount type=bind,src="$root",dst=/src,readonly \
  --mount type=volume,src="$volume",dst=/cache \
  --env npm_config_cache=/cache/npm \
  --env PUPPETEER_CACHE_DIR=/cache/puppeteer \
  --env MYFLAMES_NO_SANDBOX=1 \
  --env MYFLAMES_BROWSER_ARGS=--no-sandbox \
  "$image" /bin/bash -s -- "$suites" <<'CONTAINER'
set -euo pipefail
suites="$1"
mkdir -p /work
tar -C /src --exclude=node_modules --exclude=./myflames/ui_assets -cf - . | tar -C /work -xf -
cd /work/apps/workspace
npm ci
npx puppeteer browsers install chrome-headless-shell
# Suites launch the pinned headless shell without a sandbox switch; the
# container's kernel cannot give Chrome its sandbox, so wrap the binary.
chrome_bin="$(find /cache/puppeteer/chrome-headless-shell -name chrome-headless-shell -type f | head -1)"
printf '#!/bin/sh\nexec "%s" --no-sandbox "$@"\n' "$chrome_bin" > /usr/local/bin/chrome-no-sandbox
chmod +x /usr/local/bin/chrome-no-sandbox
export PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-no-sandbox
npm run build
cd /work
exec node scripts/run_workspace_suites.mjs --only "$suites" --no-build --jobs 1
CONTAINER
