#!/bin/bash
# Offline regression checks for Windows template lifecycle and media guards.

set -euo pipefail

WINDOWS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=../provision-host.sh
source "$WINDOWS_DIR/provision-host.sh"

declare -A CONFIGS STATUS NAMES DESCRIPTIONS TEMPLATES PROTECTIONS MACHINES
COMMANDS=

config_value() {
    local vmid=$1 key=$2
    case "$key" in
        name) printf '%s\n' "${NAMES[$vmid]-}" ;;
        description) printf '%s\n' "${DESCRIPTIONS[$vmid]-}" ;;
        template) printf '%s\n' "${TEMPLATES[$vmid]-}" ;;
        protection) printf '%s\n' "${PROTECTIONS[$vmid]-}" ;;
        machine) printf '%s\n' "${MACHINES[$vmid]-}" ;;
        *) return 1 ;;
    esac
}

qm() {
    local operation=$1 vmid=$2
    shift 2
    case "$operation" in
        config) printf '%s\n' "${CONFIGS[$vmid]-}" ;;
        status) printf 'status: %s\n' "${STATUS[$vmid]}" ;;
        agent) return 0 ;;
        set)
            if [[ "$1" == --delete ]]; then
                local slot=$2
                CONFIGS[$vmid]=$(printf '%s\n' "${CONFIGS[$vmid]}" | sed "/^${slot}:/d")
                COMMANDS+="delete:$vmid:$slot "
            fi
            ;;
        shutdown) STATUS[$vmid]=stopped ;;
        stop) STATUS[$vmid]=stopped ;;
        destroy) COMMANDS+="destroy:$vmid " ;;
        *) printf 'unexpected mocked qm operation: %s\n' "$operation" >&2; return 1 ;;
    esac
}

sleep() {
    :
}

guest_powershell() {
    local vmid=$1
    STATUS[$vmid]=stopped
}

reset_vm() {
    local vmid=$1 name=$2 template=${3:-} protection=${4:-0}
    NAMES[$vmid]=$name
    DESCRIPTIONS[$vmid]=$WINDOWS_DESCRIPTION
    TEMPLATES[$vmid]=$template
    PROTECTIONS[$vmid]=$protection
    MACHINES[$vmid]=q35
    STATUS[$vmid]=stopped
    CONFIGS[$vmid]="name: $name"
    COMMANDS=
}

assert_rejected() {
    local expected=$1
    shift
    local output
    if output=$("$@" 2>&1); then
        printf 'command unexpectedly succeeded: %s\n' "$*" >&2
        exit 1
    fi
    grep -Fq "$expected" <<<"$output" || {
        printf 'missing rejection %q in: %s\n' "$expected" "$output" >&2
        exit 1
    }
}

reset_vm 9002 proxytools-windows-template '' 0
CONFIGS[9002]=$'name: proxytools-windows-template\nide0: local:iso/windows.iso,media=cdrom\nide2: local:iso/virtio.iso,media=cdrom\nsata0: local-lvm:vm-9002-disk-0'
purge_exact_candidate 9002 proxytools-windows-template
[[ "$COMMANDS" == *'delete:9002:ide0 '* ]]
[[ "$COMMANDS" == *'delete:9002:ide2 '* ]]
[[ "$COMMANDS" == *'destroy:9002 '* ]]

reset_vm 9002 proxytools-windows-template 1 0
assert_rejected 'refusing to purge template' \
    purge_exact_candidate 9002 proxytools-windows-template
purge_exact_candidate 9002 proxytools-windows-template 1
[[ "$COMMANDS" == *'destroy:9002 '* ]]

reset_vm 9002 proxytools-windows-template '' 1
assert_rejected 'refusing to purge protected' \
    purge_exact_candidate 9002 proxytools-windows-template 1

reset_vm 9002 unrelated-vm '' 0
assert_rejected 'unexpected name' \
    purge_exact_candidate 9002 proxytools-windows-template 1

reset_vm 9002 proxytools-windows-template '' 0
CONFIGS[9002]=$'name: proxytools-windows-template\nargs: -cdrom /var/lib/vz/template/iso/unmanaged.iso'
assert_rejected 'source media remains attached' detach_cached_source_media 9002

reset_vm 9002 proxytools-windows-template 1 0
MACHINES[9002]=pc-q35-11.0+pve2
case "$(config_value 9002 machine)" in
    q35|pc-q35-*) ;;
    *) exit 1 ;;
esac
MACHINES[9002]=i440fx
case "$(config_value 9002 machine)" in
    q35|pc-q35-*) exit 1 ;;
esac

xmllint --noout "$WINDOWS_DIR/autounattend.xml"
[[ $(grep -o '@@PASSWORD@@' "$WINDOWS_DIR/autounattend.xml" | wc -l) -eq 2 ]]
COMPUTER_NAME=$(xmllint --xpath \
    'string(//*[local-name()="settings"][@pass="specialize"]/*[local-name()="component"]/*[local-name()="ComputerName"])' \
    "$WINDOWS_DIR/autounattend.xml")
[[ "$COMPUTER_NAME" =~ ^[A-Za-z0-9-]{1,15}$ ]]
[[ "$COMPUTER_NAME" != -* && "$COMPUTER_NAME" != *- ]]
[[ $(grep -o '<SkipMachineOOBE>true</SkipMachineOOBE>' \
    "$WINDOWS_DIR/autounattend.xml" | wc -l) -eq 1 ]]
[[ $(grep -o '<SkipUserOOBE>true</SkipUserOOBE>' \
    "$WINDOWS_DIR/autounattend.xml" | wc -l) -eq 1 ]]
grep -Fq 'python-3.13.15-amd64.exe' "$WINDOWS_DIR/bootstrap.ps1"
grep -Fq "$PYTHON_SHA256" <(tr '[:upper:]' '[:lower:]' <"$WINDOWS_DIR/bootstrap.ps1")
grep -Fq "$OPENSSH_INSTALLER" "$WINDOWS_DIR/bootstrap.ps1"
grep -Fq "$OPENSSH_SHA256" <(tr '[:upper:]' '[:lower:]' <"$WINDOWS_DIR/bootstrap.ps1")
grep -Fq 'ADDLOCAL=Server' "$WINDOWS_DIR/bootstrap.ps1"
grep -Fq 'stage=openssh-install' "$WINDOWS_DIR/bootstrap.ps1"
grep -Fq 'template_mode = "ready-state-v1"' "$WINDOWS_DIR/bootstrap.ps1"
grep -Fq 'Stop-Computer -Force' "$WINDOWS_DIR/bootstrap.ps1"
! grep -Eiq 'Sysprep\.exe|/generalize|/oobe' "$WINDOWS_DIR/bootstrap.ps1"
! grep -Fq 'Add-WindowsCapability' "$WINDOWS_DIR/bootstrap.ps1"
grep -Fq 'boot_key<=8' "$WINDOWS_DIR/provision-host.sh"
grep -Fq 'qm sendkey "$WINDOWS_VMID" ret' "$WINDOWS_DIR/provision-host.sh"
grep -Fq 'qm set "$WINDOWS_VMID" --boot '\''order=ide0;sata0'\''' \
    "$WINDOWS_DIR/provision-host.sh"
grep -Fq 'wait_for_ready_shutdown "$WINDOWS_VMID"' "$WINDOWS_DIR/provision-host.sh"
! grep -Fq 'sysprep-unattend.xml' "$WINDOWS_DIR/provision-host.sh"
! grep -Fq 'slmgr.vbs /ato' "$WINDOWS_DIR/bootstrap.ps1"
! grep -Riq 'chocolatey\|winget\|invoke-webrequest\|windows update' \
    "$WINDOWS_DIR/bootstrap.ps1" "$WINDOWS_DIR/autounattend.xml"

reset_vm 9002 proxytools-windows-template '' 0
STATUS[9002]=stopped
assert_rejected 'stopped before the template ready marker was observed' \
    wait_for_ready_shutdown 9002 1
STATUS[9002]=running
wait_for_ready_shutdown 9002 2
[[ "${STATUS[9002]}" == stopped ]]

MEDIA_TEST_DIR=$(mktemp -d)
trap 'rm -rf -- "$MEDIA_TEST_DIR"' EXIT
printf 'verified media\n' >"$MEDIA_TEST_DIR/source.iso"
IMAGE_DIR=$MEDIA_TEST_DIR
SOURCE_SHA256=$(sha256sum "$MEDIA_TEST_DIR/source.iso" | awk '{print $1}')
verify_cached source.iso "$SOURCE_SHA256" >/dev/null
assert_rejected 'failed its pinned SHA256' verify_cached source.iso \
    0000000000000000000000000000000000000000000000000000000000000000

printf 'Windows provision guard tests: OK\n'
