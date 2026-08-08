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
run_clean "$RUNTIME/proxytools" monitor --help | grep -F -- '--max-latency'
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
