#!/bin/sh
# Run offline smoke checks against a built one-file executable in a clean,
# disposable runtime directory.

set -eu

if [ "$#" -ne 1 ]; then
    printf 'Usage: %s /path/to/proxytools\n' "$0" >&2
    exit 2
fi

BINARY=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
test -x "$BINARY"

SMOKE_ROOT=$(mktemp -d)
cleanup() {
    chmod -R u+w "$SMOKE_ROOT" 2>/dev/null || true
    rm -rf -- "$SMOKE_ROOT"
}
trap cleanup EXIT HUP INT TERM

RUNTIME="$SMOKE_ROOT/runtime"
mkdir "$RUNTIME"
cp "$BINARY" "$RUNTIME/proxytools"

run_clean() {
    env -i HOME="$SMOKE_ROOT/home" PATH=/nonexistent "$@"
}

VERSION=$(run_clean "$RUNTIME/proxytools" --version)
test -n "$VERSION"
printf '%s\n' "$VERSION"
run_clean "$RUNTIME/proxytools" --about | grep -F "Proxy Tools $VERSION"
run_clean "$RUNTIME/proxytools" --help | grep -F 'Usage:'
run_clean "$RUNTIME/proxytools" list --help | grep -F -- '--max-latency'
test -s "$RUNTIME/proxytools.conf"
printf '\n# preserved by frozen smoke\n' >>"$RUNTIME/proxytools.conf"
run_clean "$RUNTIME/proxytools" monitor --help | grep -F -- '--max-latency'
grep -F '# preserved by frozen smoke' "$RUNTIME/proxytools.conf"

mkdir "$RUNTIME/proxydb" "$RUNTIME/geodb"
printf 'generated\n' >"$RUNTIME/geodb/version"
LOCK="$RUNTIME/proxydb/proxytools.lock"
MARKER="$SMOKE_ROOT/lock-held"
(
    flock -n 9
    : >"$MARKER"
    sleep 10
) 9>"$LOCK" &
LOCK_PID=$!
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    test -e "$MARKER" && break
    sleep 1
done
test -e "$MARKER"
if run_clean "$RUNTIME/proxytools" --clear >"$SMOKE_ROOT/locked.log" 2>&1; then
    printf 'proxytools unexpectedly cleared state while its lock was held\n' >&2
    kill "$LOCK_PID" 2>/dev/null || true
    wait "$LOCK_PID" 2>/dev/null || true
    exit 1
fi
grep -F 'refusing to clear: another proxytools process is already running' \
    "$SMOKE_ROOT/locked.log"
kill "$LOCK_PID" 2>/dev/null || true
wait "$LOCK_PID" 2>/dev/null || true
run_clean "$RUNTIME/proxytools" --clear | grep -E 'Removed|already clean'

test ! -e "$RUNTIME/.venv"
test ! -e "$RUNTIME/geodb"
test ! -e "$RUNTIME/proxydb"

READ_ONLY="$SMOKE_ROOT/read-only"
mkdir "$READ_ONLY"
cp "$BINARY" "$READ_ONLY/proxytools"
chmod 0555 "$READ_ONLY"
if run_clean "$READ_ONLY/proxytools" list --help >"$SMOKE_ROOT/read-only.log" 2>&1; then
    printf 'proxytools unexpectedly created a config in a read-only directory\n' >&2
    exit 1
fi
grep -F 'configuration error: cannot create' "$SMOKE_ROOT/read-only.log"
test ! -e "$READ_ONLY/proxytools.conf"

printf 'Local frozen smoke tests passed.\n'
