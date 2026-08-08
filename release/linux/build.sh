#!/bin/sh
# Build and validate the local Linux one-file executable.
#
# The script deliberately uses a fresh release-only virtual environment. It
# tests the current worktree, so local packaging work does not need to be
# committed before it can be exercised.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
WORK="$ROOT/release/.work/local-linux"
VENV="$WORK/venv"
DIST="$WORK/artifacts"
LOGS="$WORK/logs"
BUILD="$WORK/pyinstaller"
PYINSTALLER_VERSION=6.15.0

rm -rf -- "$WORK"
mkdir -p -- "$DIST" "$LOGS" "$BUILD"

exec 3>&1
exec >"$LOGS/build.log" 2>&1
set -x

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -e "$ROOT"
"$VENV/bin/python" -m pip install "pyinstaller==$PYINSTALLER_VERSION"

cd "$ROOT"
"$VENV/bin/python" -m unittest discover -v
find src/proxytools -name '*.py' -print0 \
    | xargs -0 "$VENV/bin/python" -m py_compile
sh -n proxytools release/linux/build.sh release/linux/smoke.sh

"$VENV/bin/pyinstaller" \
    --noconfirm \
    --clean \
    --distpath "$DIST" \
    --workpath "$BUILD" \
    "$ROOT/release/pyinstaller/proxytools.spec"

cp "$ROOT/README.md" "$DIST/README.md"
chmod 0755 "$DIST/proxytools"

{
    printf 'source_commit=%s\n' "$(git rev-parse HEAD)"
    if test -n "$(git status --porcelain)"; then
        printf 'source_tree=dirty\n'
    else
        printf 'source_tree=clean\n'
    fi
    printf 'build_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'os=%s\n' "$(. /etc/os-release; printf '%s' "$PRETTY_NAME")"
    printf 'glibc=%s\n' "$(ldd --version | head -n 1)"
    printf 'python=%s\n' "$("$VENV/bin/python" --version 2>&1)"
    printf 'pyinstaller=%s\n' "$("$VENV/bin/pyinstaller" --version)"
    (cd "$DIST" && sha256sum proxytools README.md)
} >"$DIST/MANIFEST.txt"

"$ROOT/release/linux/smoke.sh" "$DIST/proxytools" \
    >"$LOGS/smoke.log" 2>&1

printf 'Linux artifact: %s\n' "$DIST/proxytools" >&3
printf 'Manifest: %s\n' "$DIST/MANIFEST.txt" >&3
printf 'Logs: %s\n' "$LOGS" >&3
