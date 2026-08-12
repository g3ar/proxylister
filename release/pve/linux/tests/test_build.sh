#!/bin/bash
# Offline regression checks for destructive guards in the PVE build driver.

set -euo pipefail

PVE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=../build.sh
source "$PVE_DIR/build.sh"

declare -A NAMES TEMPLATES PROTECTIONS STATUSES CONFIGS
DESTROYED=
DETACHED=
TEST_ROOT=$(mktemp -d /tmp/proxytools-pve-build-test.XXXXXX)

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
                printf ' %s %s stopped\n' "$vmid" "${NAMES[$vmid]}"
            done
            ;;
        'qm status '*)
            vmid=${command#qm status }
            printf 'status: %s\n' "${STATUSES[$vmid]:-stopped}"
            ;;
        'qm shutdown '*)
            vmid=${command#qm shutdown }
            vmid=${vmid%% *}
            STATUSES[$vmid]=stopped
            ;;
        'qm config '*)
            vmid=${command#qm config }
            printf 'name: %s\n' "${NAMES[$vmid]-}"
            printf 'protection: %s\n' "${PROTECTIONS[$vmid]-}"
            [[ -z "${TEMPLATES[$vmid]-}" ]] \
                || printf 'template: %s\n' "${TEMPLATES[$vmid]}"
            [[ -z "${CONFIGS[$vmid]-}" ]] \
                || printf '%s\n' "${CONFIGS[$vmid]}"
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
                printf 'unexpected mocked qm set command: %s\n' "$command" >&2
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

reset_vm 101 proxytools-debian-build-101 '' 0 running
cleanup_stale_clones
[[ "$DESTROYED" == 101 ]]

reset_vm 102 proxytools-ubuntu-validation-102 '' 0 stopped
cleanup_stale_clones
[[ "$DESTROYED" == 102 ]]

assert_rejected() {
    local expected=$1
    shift
    if ( "$@" ) >"$TEST_ROOT/rejected.out" 2>&1; then
        printf 'expected rejection containing: %s\n' "$expected" >&2
        exit 1
    fi
    grep -q "$expected" "$TEST_ROOT/rejected.out"
}

reset_vm 103 proxytools-debian-build-999 '' 0 stopped
assert_rejected 'unexpected name' cleanup_stale_clones
reset_vm 104 proxytools-debian-build-104 1 0 stopped
assert_rejected 'refusing to delete template' cleanup_stale_clones
reset_vm 105 proxytools-debian-build-105 '' 1 stopped
assert_rejected 'refusing to delete protected' cleanup_stale_clones
assert_rejected 'protected template VMID' remove_owned_clone 9000 proxytools-linux-template

reset_vm 106 proxytools-debian-build-106 '' 0 stopped
CONFIGS[106]=$'ide0: local:iso/debian-installer.iso,media=cdrom\nsata1: /var/lib/vz/template/iso/virtio-win.iso,media=cdrom\nide2: local-lvm:cloudinit,media=cdrom'
remove_owned_clone 106 proxytools-debian-build-106
[[ "$DETACHED" == '106:ide0 106:sata1' ]]
[[ "$DESTROYED" == 106 ]]
[[ "${CONFIGS[106]}" == 'ide2: local-lvm:cloudinit,media=cdrom' ]]

reset_vm 107 proxytools-debian-build-107 '' 0 stopped
CONFIGS[107]='args: -cdrom /var/lib/vz/template/iso/unmanaged.iso'
assert_rejected 'config references cached source media' \
    remove_owned_clone 107 proxytools-debian-build-107
[[ -z "$DESTROYED" ]]

SOURCE_REPO="$TEST_ROOT/source"
mkdir -p "$SOURCE_REPO"
git -C "$SOURCE_REPO" init -q
git -C "$SOURCE_REPO" config user.name 'Proxy Tools test'
git -C "$SOURCE_REPO" config user.email 'proxytools-test.invalid'
printf 'same source for both guests\n' >"$SOURCE_REPO/input.txt"
git -C "$SOURCE_REPO" add input.txt
git -C "$SOURCE_REPO" commit -qm snapshot
(
    cd "$SOURCE_REPO"
    WORK="$TEST_ROOT/release-work"
    SOURCE_ARCHIVE="$WORK/source.tar"
    SOURCE_CHECKSUM="$WORK/source.tar.sha256"
    mkdir -p "$WORK"
    prepare_release_source
    (cd "$WORK" && sha256sum -c "$(basename "$SOURCE_CHECKSUM")")
    expected=$(awk '{print $1}' "$SOURCE_CHECKSUM")
    [[ "$(sha256sum "$SOURCE_ARCHIVE" | awk '{print $1}')" == "$expected" ]]
    touch dirty-file
    assert_rejected 'clean worktree' prepare_release_source
)

printf 'PVE build guard tests: OK\n'
