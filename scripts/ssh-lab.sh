#!/usr/bin/env bash
# scripts/ssh-lab.sh
#
# A throwaway OpenSSH/SFTP server in Docker for the Files place's live tests
# (test/test_files_live.py). The lab user "lab" signs in with a password, with
# a plain key and with a passphrase-protected key; keys and an env file are
# written under local/ssh-lab (gitignored). Nothing is written to ~/.ssh: the
# tests trust the lab's host key through myflames' own known_hosts file.
#
# Usage:
#   scripts/ssh-lab.sh            # up -> test -> down
#   scripts/ssh-lab.sh up         # build and start the lab, write local/ssh-lab/lab.env
#   scripts/ssh-lab.sh test       # test_files_live.py and apps/workspace/test/files-live.mjs
#   scripts/ssh-lab.sh down       # remove the container
#   scripts/ssh-lab.sh env        # print the exports the live tests read

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAB="${SSH_LAB:-$REPO/local/ssh-lab}"
IMAGE="myflames-sshlab:1"
CONTAINER="myflames-sshlab"
PORT="${SSH_LAB_PORT:-2222}"

up() {
  mkdir -p "$LAB"
  chmod 700 "$LAB"
  docker build -q -t "$IMAGE" "$REPO/scripts/ssh-lab" >/dev/null
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" -p "127.0.0.1:$PORT:22" "$IMAGE" >/dev/null
  rm -f "$LAB/id_plain" "$LAB/id_plain.pub" "$LAB/id_phrase" "$LAB/id_phrase.pub"
  ssh-keygen -q -t ed25519 -N '' -C 'myflames lab plain' -f "$LAB/id_plain"
  ssh-keygen -q -t ed25519 -N 'labphrase' -C 'myflames lab phrase' -f "$LAB/id_phrase"
  cat "$LAB/id_plain.pub" "$LAB/id_phrase.pub" | docker exec -i "$CONTAINER" sh -c \
    'cat > /home/lab/.ssh/authorized_keys && chown lab:lab /home/lab/.ssh/authorized_keys && chmod 600 /home/lab/.ssh/authorized_keys'
  for _ in $(seq 1 50); do
    if ssh-keyscan -p "$PORT" -T 2 127.0.0.1 >/dev/null 2>&1; then break; fi
    sleep 0.2
  done
  cat > "$LAB/lab.env" <<EOF
export MYFLAMES_SSH_LAB=127.0.0.1:$PORT
export MYFLAMES_SSH_LAB_USER=lab
export MYFLAMES_SSH_LAB_PASSWORD=labpass
export MYFLAMES_SSH_LAB_KEY=$LAB/id_plain
export MYFLAMES_SSH_LAB_PHRASE_KEY=$LAB/id_phrase
export MYFLAMES_SSH_LAB_PASSPHRASE=labphrase
export MYFLAMES_SSH_LAB_CONTAINER=$CONTAINER
EOF
  echo "SSH lab up on 127.0.0.1:$PORT (env: $LAB/lab.env)"
}

run_tests() {
  # shellcheck disable=SC1091
  . "$LAB/lab.env"
  cd "$REPO"
  python3 -m unittest discover -s test -p "test_files_live.py" -v
  # The Files place end to end in a browser, against the built workspace.
  (cd apps/workspace && npm run build >/dev/null && node test/files-live.mjs)
}

down() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  echo "SSH lab removed"
}

case "${1:-all}" in
  up) up ;;
  test) run_tests ;;
  down) down ;;
  env) cat "$LAB/lab.env" ;;
  all) up; trap down EXIT; run_tests ;;
  *) echo "usage: $0 [up|test|down|env]" >&2; exit 2 ;;
esac
