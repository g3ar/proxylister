#!/bin/bash
# Offline regression checks for Windows PVE build lifecycle and cleanup guards.

set -euo pipefail

WINDOWS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=../build.sh
source "$WINDOWS_DIR/build.sh"

declare -A NAMES TEMPLATES PROTECTIONS STATUSES CONFIGS
DESTROYED=
DETACHED=
TEST_ROOT=$(mktemp -d /tmp/proxytools-pve-windows-build-test.XXXXXX)

cleanup_test() {
    rm -rf -- "$TEST_ROOT"
}
trap cleanup_test EXIT

config_value() {
    local vmid=$1 key=$2
    case "$key" in
        name) printf '%s\n' "${NAMES[$vmid]-}" ;;
        template) printf '%s\n' "${TEMPLATES[$vmid]-}" ;;
        protection) printf '%s\n' "${PROTECTIONS[$vmid]-}" ;;
    esac
}

pve() {
    local command=$1 vmid
    case "$command" in
        'qm list')
            printf ' VMID NAME STATUS\n'
            for vmid in "${!NAMES[@]}"; do
                printf ' %s %s %s\n' "$vmid" "${NAMES[$vmid]}" "${STATUSES[$vmid]:-stopped}"
            done
            ;;
        'qm status '*)
            vmid=${command#qm status }
            printf 'status: %s\n' "${STATUSES[$vmid]:-stopped}"
            ;;
        'qm shutdown '*|'qm stop '*)
            vmid=${command#qm shutdown }
            vmid=${vmid#qm stop }
            vmid=${vmid%% *}
            STATUSES[$vmid]=stopped
            ;;
        'qm config '*)
            vmid=${command#qm config }
            printf 'name: %s\n' "${NAMES[$vmid]-}"
            printf 'protection: %s\n' "${PROTECTIONS[$vmid]-}"
            [[ -z "${TEMPLATES[$vmid]-}" ]] || printf 'template: %s\n' "${TEMPLATES[$vmid]}"
            [[ -z "${CONFIGS[$vmid]-}" ]] || printf '%s\n' "${CONFIGS[$vmid]}"
            ;;
        'qm set '*' --delete '*)
            if [[ "$command" =~ ^qm\ set\ ([0-9]+)\ --delete\ ([a-z]+[0-9]+)$ ]]; then
                local slot=${BASH_REMATCH[2]} line remaining=
                vmid=${BASH_REMATCH[1]}
                while IFS= read -r line; do
                    [[ "${line%%:*}" == "$slot" ]] && continue
                    remaining+="${remaining:+$'\n'}$line"
                done <<<"${CONFIGS[$vmid]-}"
                CONFIGS[$vmid]=$remaining
                DETACHED+="${DETACHED:+ }$vmid:$slot"
            else
                printf 'unexpected mocked qm set: %s\n' "$command" >&2
                return 1
            fi
            ;;
        'qm destroy '*)
            vmid=${command#qm destroy }
            DESTROYED=${vmid%% *}
            ;;
        *)
            printf 'unexpected mocked PVE command: %s\n' "$command" >&2
            return 1
            ;;
    esac
}

reset_vm() {
    local vmid=$1 name=$2 template=${3:-} protection=${4:-0} status=${5:-stopped}
    NAMES=() TEMPLATES=() PROTECTIONS=() STATUSES=() CONFIGS=()
    NAMES[$vmid]=$name
    TEMPLATES[$vmid]=$template
    PROTECTIONS[$vmid]=$protection
    STATUSES[$vmid]=$status
    DESTROYED=
    DETACHED=
}

assert_rejected() {
    local expected=$1
    shift
    if ( "$@" ) >"$TEST_ROOT/rejected.out" 2>&1; then
        printf 'expected rejection containing: %s\n' "$expected" >&2
        exit 1
    fi
    grep -q "$expected" "$TEST_ROOT/rejected.out"
}

reset_vm 101 proxytools-windows-build-101 '' 0 running
cleanup_stale_clones
[[ "$DESTROYED" == 101 ]]

reset_vm 102 proxytools-windows-build-999 '' 0 stopped
assert_rejected 'unexpected name' cleanup_stale_clones

reset_vm 103 proxytools-windows-build-103 1 0 stopped
assert_rejected 'refusing to delete template' cleanup_stale_clones

reset_vm 104 proxytools-windows-build-104 '' 1 stopped
assert_rejected 'refusing to delete protected' cleanup_stale_clones
assert_rejected 'Windows template VMID' remove_owned_clone 9002 proxytools-windows-template

reset_vm 105 proxytools-windows-build-105 '' 0 stopped
CONFIGS[105]=$'ide0: local:iso/windows.iso,media=cdrom\nsata2: /var/lib/vz/template/iso/virtio.iso,media=cdrom\nsata0: local-lvm:vm-105-disk-0'
remove_owned_clone 105 proxytools-windows-build-105
[[ "$DETACHED" == '105:ide0 105:sata2' ]]
[[ "$DESTROYED" == 105 ]]
[[ "${CONFIGS[105]}" == 'sata0: local-lvm:vm-105-disk-0' ]]

reset_vm 106 proxytools-windows-build-106 '' 0 stopped
CONFIGS[106]='args: -cdrom /var/lib/vz/template/iso/unmanaged.iso'
assert_rejected 'cached source media remains attached' \
    remove_owned_clone 106 proxytools-windows-build-106
[[ -z "$DESTROYED" ]]

SOURCE_REPO="$TEST_ROOT/source"
mkdir -p "$SOURCE_REPO/release/bin/windows" "$SOURCE_REPO/release/.work/old" "$SOURCE_REPO/src"
git -C "$SOURCE_REPO" init -q
git -C "$SOURCE_REPO" config user.name 'Proxy Tools test'
git -C "$SOURCE_REPO" config user.email 'proxytools-test.invalid'
printf 'source\n' >"$SOURCE_REPO/src/input.txt"
git -C "$SOURCE_REPO" add src/input.txt
git -C "$SOURCE_REPO" commit -qm snapshot
printf 'ignored artifact\n' >"$SOURCE_REPO/release/bin/windows/proxytools.exe"
printf 'ignored work\n' >"$SOURCE_REPO/release/.work/old/log"
(
    ROOT="$SOURCE_REPO"
    WORK="$TEST_ROOT/archive-work"
    SOURCE_ARCHIVE="$WORK/source.tar"
    SOURCE_CHECKSUM="$WORK/source.tar.sha256"
    RELEASE_MODE=0
    mkdir -p "$WORK"
    prepare_source
    tar -tf "$SOURCE_ARCHIVE" >"$TEST_ROOT/archive-list"
    grep -q './src/input.txt' "$TEST_ROOT/archive-list"
    ! grep -q 'release/bin' "$TEST_ROOT/archive-list"
    ! grep -q 'release/.work' "$TEST_ROOT/archive-list"
    (cd "$WORK" && sha256sum -c source.tar.sha256)
)

grep -Fq 'BIN="$BIN_ROOT/windows"' "$WINDOWS_DIR/build.sh"
! grep -Eq 'rm -rf -- .*BIN_ROOT' "$WINDOWS_DIR/build.sh"
[[ "$(grep -c 'scp -O -F /dev/null' "$WINDOWS_DIR/build.sh")" == 2 ]]
grep -Fq 'portalocker==4.1.0' "$WINDOWS_DIR/constraints.txt"
grep -Fq 'Windows frozen smoke tests passed.' "$WINDOWS_DIR/smoke.ps1"
grep -Fq 'Windows live frozen smoke tests passed.' "$WINDOWS_DIR/smoke-live.ps1"
grep -Fq 'slmgr.vbs /ato' "$WINDOWS_DIR/build.sh"
grep -Fq 'GracePeriodRemaining -le 0' "$WINDOWS_DIR/build.sh"
grep -Fq 'activate_guest "$ACTIVE_IP"' "$WINDOWS_DIR/build.sh"

printf 'Windows PVE build guard tests: OK\n'
