#!/bin/sh
# Run bounded, network-dependent checks against a built Linux executable.
# This script is intentionally separate from the deterministic build gate.

set -eu

if [ "$#" -ne 1 ]; then
    printf 'Usage: %s /path/to/proxytools\n' "$0" >&2
    exit 2
fi

BINARY=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
test -x "$BINARY"

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
LOGS="$ROOT/release/.work/local-linux/logs"
mkdir -p -- "$LOGS"
rm -f -- "$LOGS/live-list.log" "$LOGS/live-monitor.log"

LIVE_ROOT=$(mktemp -d)
cleanup() {
    chmod -R u+w "$LIVE_ROOT" 2>/dev/null || true
    rm -rf -- "$LIVE_ROOT"
}
trap cleanup EXIT HUP INT TERM

RUNTIME="$LIVE_ROOT/runtime"
mkdir "$RUNTIME"
cp "$BINARY" "$RUNTIME/proxytools"

LIST_TIMEOUT=${PROXYTOOLS_LIVE_LIST_TIMEOUT:-300}
MONITOR_TIMEOUT=${PROXYTOOLS_LIVE_MONITOR_TIMEOUT:-60}

timeout "$LIST_TIMEOUT" "$RUNTIME/proxytools" list \
    >"$LOGS/live-list.log" 2>&1 || {
        printf 'Live list smoke failed; output follows:\n' >&2
        sed -n '1,240p' "$LOGS/live-list.log" >&2
        exit 1
    }
test -s "$RUNTIME/geodb/geoip.mmdb"
test -e "$RUNTIME/working_proxies.txt"

if ! printf 'q' | timeout "$MONITOR_TIMEOUT" \
    script -qefc "$RUNTIME/proxytools monitor" /dev/null \
    >"$LOGS/live-monitor.log" 2>&1; then
    printf 'Live monitor smoke failed; output follows:\n' >&2
    sed -n '1,240p' "$LOGS/live-monitor.log" >&2
    exit 1
fi

test -s "$RUNTIME/proxydb/proxytools.db"
printf 'Optional live frozen smoke tests passed.\n'
