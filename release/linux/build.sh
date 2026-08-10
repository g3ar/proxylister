#!/bin/sh
# Build and validate the local Linux one-file executable.
#
# The script deliberately uses a fresh release-only virtual environment. It
# tests the current worktree, so local packaging work does not need to be
# committed before it can be exercised.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
WORK="$ROOT/release/.work/local-linux"
BIN="$ROOT/release/bin"
VENV="$WORK/venv"
DIST="$WORK/artifacts"
LOGS="$WORK/logs"
BUILD="$WORK/pyinstaller"
CONSTRAINTS="$ROOT/release/linux/constraints.txt"

rm -rf -- "$WORK" "$BIN"
mkdir -p -- "$DIST" "$LOGS" "$BUILD"

exec 3>&1
exec >"$LOGS/build.log" 2>&1
set -x

python3 -m venv "$VENV"
PIP_CONSTRAINT="$CONSTRAINTS"
export PIP_CONSTRAINT
"$VENV/bin/python" -m pip install --upgrade pip setuptools
"$VENV/bin/python" -m pip install -e "$ROOT" pyinstaller

grep -Ev '^[[:space:]]*(#|$)' "$CONSTRAINTS" \
    | LC_ALL=C sort -f >"$WORK/expected-packages.txt"
"$VENV/bin/python" -m pip freeze --all --exclude-editable \
    | LC_ALL=C sort -f >"$WORK/resolved-packages.txt"
diff -u "$WORK/expected-packages.txt" "$WORK/resolved-packages.txt"

BUILD_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SOURCE_COMMIT=${PROXYTOOLS_SOURCE_COMMIT:-$(git rev-parse HEAD)}
SOURCE_TREE=${PROXYTOOLS_SOURCE_TREE:-}
if test -z "$SOURCE_TREE"; then
    if test -n "$(git status --porcelain)"; then
        SOURCE_TREE=dirty
    else
        SOURCE_TREE=clean
    fi
fi
case "$SOURCE_TREE" in
    clean|dirty) ;;
    *)
        printf 'Invalid source tree state: %s\n' "$SOURCE_TREE" >&2
        exit 1
        ;;
esac
PROXYTOOLS_BUILD_INFO="$WORK/proxytools-build.txt"
export PROXYTOOLS_BUILD_INFO
{
    printf 'build_utc=%s\n' "$BUILD_UTC"
    printf 'source_commit=%s\n' "$SOURCE_COMMIT"
} >"$PROXYTOOLS_BUILD_INFO"

cd "$ROOT"
"$VENV/bin/python" -m unittest discover -v
find src/proxytools -name '*.py' -print0 \
    | xargs -0 "$VENV/bin/python" -m py_compile
sh -n proxytools release/linux/build.sh release/linux/smoke.sh \
    release/linux/smoke-live.sh

"$VENV/bin/pyinstaller" \
    --noconfirm \
    --clean \
    --distpath "$DIST" \
    --workpath "$BUILD" \
    "$ROOT/release/pyinstaller/proxytools.spec"

cp "$ROOT/README.md" "$DIST/README.md"
cp "$ROOT/LICENSE" "$DIST/LICENSE"
chmod 0755 "$DIST/proxytools"

VERSION=$("$DIST/proxytools" --version)
case "$VERSION" in
    ''|*[!0-9A-Za-z.-]*)
        printf 'Invalid artifact version: %s\n' "$VERSION" >&2
        exit 1
        ;;
esac
ARTIFACT=proxytools

{
    printf 'artifact=%s\n' "$ARTIFACT"
    printf 'version=%s\n' "$VERSION"
    printf 'source_commit=%s\n' "$SOURCE_COMMIT"
    printf 'source_tree=%s\n' "$SOURCE_TREE"
    printf 'build_utc=%s\n' "$BUILD_UTC"
    printf 'os=%s\n' "$(. /etc/os-release; printf '%s' "$PRETTY_NAME")"
    printf 'architecture=%s\n' "$(uname -m)"
    printf 'glibc=%s\n' "$(ldd --version | head -n 1)"
    printf 'python=%s\n' "$("$VENV/bin/python" --version 2>&1)"
    printf 'pip=%s\n' "$("$VENV/bin/python" -m pip --version | cut -d ' ' -f 2)"
    printf 'pyinstaller=%s\n' "$("$VENV/bin/pyinstaller" --version)"
    printf 'constraints_sha256=%s\n' "$(sha256sum "$CONSTRAINTS" | cut -d ' ' -f 1)"
} >"$DIST/MANIFEST.txt"

(
    cd "$DIST"
    sha256sum "$ARTIFACT" README.md LICENSE MANIFEST.txt >SHA256SUMS
)

"$ROOT/release/linux/smoke.sh" "$DIST/$ARTIFACT" \
    >"$LOGS/smoke.log" 2>&1

mv -- "$DIST" "$BIN"

printf 'Linux artifact: %s\n' "$BIN/$ARTIFACT" >&3
printf 'Manifest: %s\n' "$BIN/MANIFEST.txt" >&3
printf 'Checksums: %s\n' "$BIN/SHA256SUMS" >&3
printf 'Logs: %s\n' "$LOGS" >&3
