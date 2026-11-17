#!/bin/sh
# Writes /tmp/my.cnf from the read-only lab credentials, then runs the
# line-1 identity mismatch scenario against mf-ic-1 (see _container_mismatch.py).
umask 077
mkdir -p "${HOME:-/tmp/home}"
pw=$(sed -n 's/^MF_ADMIN_PASSWORD=//p' /run/creds.env | sed "s/^['\"]//; s/['\"]$//")
printf '[client]\nuser=mf_admin\npassword=%s\nhost=mf-ic-1\nport=3306\n' "$pw" > /tmp/my.cnf
python3 -I /run/live/_container_mismatch.py 2>&1 | grep -v "LC_ALL"
