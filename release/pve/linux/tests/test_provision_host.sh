#!/bin/bash
# Verify that provision-host.sh --check-only leaves the PVE build lab unchanged.

set -euo pipefail

PVE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PVE_HOST=${PROXYTOOLS_PVE_HOST:-root@192.168.66.2}
PVE_ROOT_KEY=${PROXYTOOLS_PVE_ROOT_KEY:-$HOME/.ssh/id_rsa}
REMOTE_SCRIPT="/tmp/proxytools-provision-read-only-$$.sh"

SSH=(
    ssh -F /dev/null -i "$PVE_ROOT_KEY"
    -o BatchMode=yes -o ConnectTimeout=10
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$HOME/.ssh/known_hosts"
)
SCP=(
    scp -F /dev/null -i "$PVE_ROOT_KEY"
    -o BatchMode=yes -o ConnectTimeout=10
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$HOME/.ssh/known_hosts"
)

cleanup() {
    "${SSH[@]}" "$PVE_HOST" "rm -f -- $REMOTE_SCRIPT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

snapshot() {
    "${SSH[@]}" "$PVE_HOST" '
        set -eu
        sha256sum /etc/lvm/lvm.conf /etc/pve/storage.cfg
        stat -c "%n %a %U:%G %s %Y" \
            /var/lib/vz/template/iso /var/lib/vz/snippets
        qm list
        qm config 9000
        qm config 9001
        systemctl is-active lvm2-monitor.service
        if test -e /run/lock/proxytools-pve-build.lock; then
            stat -c "lock %a %U:%G %s %Y" /run/lock/proxytools-pve-build.lock
            sha256sum /run/lock/proxytools-pve-build.lock
        else
            printf "lock absent\n"
        fi
    '
}

for command in scp ssh; do
    command -v "$command" >/dev/null \
        || { printf 'PVE provision test: required command not found: %s\n' "$command" >&2; exit 1; }
done
[[ -f "$PVE_ROOT_KEY" ]] \
    || { printf 'PVE provision test: key not found: %s\n' "$PVE_ROOT_KEY" >&2; exit 1; }

before=$(snapshot)
"${SCP[@]}" "$PVE_DIR/provision-host.sh" "$PVE_HOST:$REMOTE_SCRIPT" >/dev/null
"${SSH[@]}" "$PVE_HOST" "chmod 0700 $REMOTE_SCRIPT && $REMOTE_SCRIPT --check-only"
after=$(snapshot)

if [[ "$before" != "$after" ]]; then
    diff -u <(printf '%s\n' "$before") <(printf '%s\n' "$after") >&2 || true
    printf 'PVE provision test: --check-only changed build-lab state\n' >&2
    exit 1
fi

printf 'PVE provision read-only test: OK\n'
