#!/bin/bash
# Provision the reusable Windows build template on the dedicated PVE host.
#
# Run as root on PVE after copying this directory and the dedicated guest
# public key. The script downloads only pinned official artifacts, verifies
# SHA256 before use, performs a minimal unattended install, validates a linked
# clone, and protects VMID 9002. Verified source media is never deleted.

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WINDOWS_VMID=9002
WINDOWS_NAME=proxytools-windows-template
WINDOWS_DESCRIPTION='Windows 11 Enterprise Evaluation 25H2 immutable Proxy Tools ready-state-v1 builder template; official pinned media; no cumulative update pass'
WINDOWS_MEMORY=4096
WINDOWS_DISK_GB=48
IMAGE_DIR=/var/lib/vz/template/iso
LOCK_FILE=/run/lock/proxytools-pve-build.lock
SSH_PUBLIC_KEY=
CHECK_ONLY=0
ACTIVE_VMID=
ANSWER_DIR=

WINDOWS_ISO=windows-11-enterprise-eval-25h2-en-us.iso
WINDOWS_URL=https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/26200.6584.250915-1905.25h2_ge_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso
WINDOWS_SHA256=a61adeab895ef5a4db436e0a7011c92a2ff17bb0357f58b13bbc4062e535e7b9
VIRTIO_ISO=virtio-win-0.1.285.iso
VIRTIO_URL=https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.285-1/virtio-win-0.1.285.iso
VIRTIO_SHA256=e14cf2b94492c3e925f0070ba7fdfedeb2048c91eea9c5a5afb30232a3976331
PYTHON_INSTALLER=python-3.13.15-amd64.exe
PYTHON_URL=https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe
PYTHON_SHA256=edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403
OPENSSH_INSTALLER=OpenSSH-Win64-v10.0.0.0.msi
OPENSSH_URL=https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/OpenSSH-Win64-v10.0.0.0.msi
OPENSSH_SHA256=ddec9c53864280759cf9f74791cefd387100e3946aa849a1c138a4ed1b96b7d9
ANSWER_ISO=proxytools-windows-unattend-9002.iso

usage() {
    cat <<'EOF'
Usage:
  ./provision-host.sh --ssh-public-key /path/to/proxytools-build.pub
  ./provision-host.sh --check-only

Normal mode creates or reconciles only the exact unprotected VMID 9002
candidate. A stopped, protected, valid template is audited and preserved.
Official Windows, VirtIO, Win32-OpenSSH, and Python source artifacts remain
cached on PVE.
EOF
}

fail() {
    printf 'windows-provision: %s\n' "$*" >&2
    exit 1
}

on_exit() {
    local status=$?
    [[ -z "$ANSWER_DIR" ]] || rm -rf -- "$ANSWER_DIR"
    if (( status != 0 )) && [[ -n "$ACTIVE_VMID" ]]; then
        printf 'windows-provision: candidate retained for diagnosis: VMID=%s\n' \
            "$ACTIVE_VMID" >&2
        printf 'windows-provision: inspect with: qm status %s; qm config %s\n' \
            "$ACTIVE_VMID" "$ACTIVE_VMID" >&2
    fi
}

config_value() {
    local vmid=$1 key=$2
    qm config "$vmid" | sed -n "s/^${key}: //p"
}

vm_exists() {
    test -e "/etc/pve/qemu-server/$1.conf"
}

references_cached_source_media() {
    local line=$1
    [[ "$line" == *':iso/'* || "$line" == *'/var/lib/vz/template/iso/'* ]]
}

detach_cached_source_media() {
    local vmid=$1 config line slot

    config=$(qm config "$vmid")
    while IFS= read -r line; do
        references_cached_source_media "$line" || continue
        slot=${line%%:*}
        if [[ "$slot" =~ ^(ide|sata|scsi|virtio|unused)[0-9]+$ ]]; then
            qm set "$vmid" --delete "$slot"
            printf 'Detached source media from VMID %s slot %s.\n' "$vmid" "$slot"
        fi
    done <<<"$config"

    config=$(qm config "$vmid")
    while IFS= read -r line; do
        references_cached_source_media "$line" || continue
        fail "refusing to purge VMID $vmid while source media remains attached: $line"
    done <<<"$config"
}

wait_for_stopped() {
    local vmid=$1 attempts=$2 attempt
    for (( attempt=1; attempt<=attempts; attempt++ )); do
        [[ "$(qm status "$vmid")" == 'status: stopped' ]] && return
        sleep 5
    done
    fail "VMID $vmid did not stop within the bounded wait"
}

stop_vm() {
    local vmid=$1 status
    status=$(qm status "$vmid")
    if [[ "$status" == 'status: running' ]]; then
        qm shutdown "$vmid" --timeout 180 || qm stop "$vmid" --overrule-shutdown 1
        wait_for_stopped "$vmid" 60
    elif [[ "$status" != 'status: stopped' ]]; then
        fail "VMID $vmid has unsupported status: $status"
    fi
}

purge_exact_candidate() {
    local vmid=$1 expected_name=$2 allow_template=${3:-0}

    [[ "$vmid" == "$WINDOWS_VMID" || "$expected_name" == proxytools-windows-validation-* ]] \
        || fail "refusing to purge unexpected VMID $vmid"
    [[ "$(config_value "$vmid" name)" == "$expected_name" ]] \
        || fail "VMID $vmid has an unexpected name"
    [[ "$(config_value "$vmid" protection)" == 0 ]] \
        || fail "refusing to purge protected VMID $vmid"
    if [[ "$(config_value "$vmid" template)" == 1 && "$allow_template" != 1 ]]; then
        fail "refusing to purge template VMID $vmid"
    fi
    stop_vm "$vmid"
    detach_cached_source_media "$vmid"
    qm destroy "$vmid" --purge 1
    printf 'Deleted unverified Windows candidate %s (%s).\n' "$vmid" "$expected_name"
}

verify_cached() {
    local filename=$1 expected=$2 target actual
    target="$IMAGE_DIR/$filename"
    [[ -f "$target" ]] || fail "required cached official artifact is missing: $target"
    actual=$(sha256sum "$target" | awk '{print $1}')
    [[ "$actual" == "$expected" ]] \
        || fail "cached official artifact failed its pinned SHA256: $target"
    printf 'Verified cached source: %s\n' "$target"
}

verified_download() {
    local filename=$1 url=$2 expected=$3 target partial actual
    target="$IMAGE_DIR/$filename"
    partial="$target.part"

    if [[ -f "$target" ]]; then
        verify_cached "$filename" "$expected"
        return
    fi
    curl -fL --retry 5 --continue-at - --output "$partial" "$url" \
        || fail "official download failed; partial file retained: $partial"
    actual=$(sha256sum "$partial" | awk '{print $1}')
    [[ "$actual" == "$expected" ]] \
        || fail "official download failed pinned SHA256; partial file retained: $partial"
    mv -- "$partial" "$target"
    printf 'Downloaded and verified official source: %s\n' "$target"
}

validate_template() {
    local require_protection=${1:-1} config

    [[ "$(qm status "$WINDOWS_VMID")" == 'status: stopped' ]] \
        || fail "Windows template must be stopped"
    [[ "$(config_value "$WINDOWS_VMID" name)" == "$WINDOWS_NAME" ]] \
        || fail "Windows template has an unexpected name"
    [[ "$(config_value "$WINDOWS_VMID" description)" == "$WINDOWS_DESCRIPTION" ]] \
        || fail "Windows template does not use the ready-state-v1 contract"
    [[ "$(config_value "$WINDOWS_VMID" template)" == 1 ]] \
        || fail "VMID $WINDOWS_VMID is not a template"
    if [[ "$require_protection" == 1 ]]; then
        [[ "$(config_value "$WINDOWS_VMID" protection)" == 1 ]] \
            || fail "Windows template is not protected"
    fi
    [[ "$(config_value "$WINDOWS_VMID" cores)" == 2 ]] \
        || fail "Windows template must have 2 cores"
    [[ "$(config_value "$WINDOWS_VMID" memory)" == "$WINDOWS_MEMORY" ]] \
        || fail "Windows template has unexpected memory"
    [[ "$(config_value "$WINDOWS_VMID" bios)" == ovmf ]] \
        || fail "Windows template must use OVMF"
    case "$(config_value "$WINDOWS_VMID" machine)" in
        q35|pc-q35-*) ;;
        *) fail "Windows template must use q35" ;;
    esac
    config=$(qm config "$WINDOWS_VMID")
    grep -q '^agent: enabled=1' <<<"$config" || fail 'QEMU Guest Agent must be enabled'
    grep -q '^efidisk0: local-lvm:base-' <<<"$config" || fail 'EFI disk is missing'
    grep -q '^tpmstate0: local-lvm:base-' <<<"$config" || fail 'TPM 2.0 state is missing'
    grep -q '^sata0: local-lvm:base-' <<<"$config" || fail 'Windows base disk is missing'
    while IFS= read -r line; do
        references_cached_source_media "$line" \
            && fail "Windows template still references source media: $line"
    done <<<"$config"
    printf 'Validated Windows template %s (%s).\n' "$WINDOWS_VMID" "$WINDOWS_NAME"
}

wait_for_agent() {
    local vmid=$1 attempt
    for (( attempt=1; attempt<=360; attempt++ )); do
        if qm agent "$vmid" ping >/dev/null 2>&1; then
            return
        fi
        sleep 5
    done
    fail "QEMU Guest Agent did not become ready in VMID $vmid"
}

wait_for_ready_shutdown() {
    local vmid=$1 attempts=$2 attempt status ready=0
    for (( attempt=1; attempt<=attempts; attempt++ )); do
        status=$(qm status "$vmid")
        if [[ "$status" == 'status: stopped' ]]; then
            (( ready )) || fail "VMID $vmid stopped before the template ready marker was observed"
            return
        fi
        [[ "$status" == 'status: running' ]] \
            || fail "VMID $vmid has unsupported status while provisioning: $status"
        if qm agent "$vmid" ping >/dev/null 2>&1 \
                && guest_powershell "$vmid" \
                    'if (-not (Test-Path C:\proxytools-template-ready.json)) { exit 1 }' \
                    >/dev/null 2>&1; then
            ready=1
        fi
        sleep 5
    done
    fail "VMID $vmid did not report readiness and stop within the bounded wait"
}

guest_powershell() {
    local vmid=$1 command=$2 result
    result=$(qm guest exec "$vmid" -- powershell.exe -NoLogo -NoProfile \
        -NonInteractive -ExecutionPolicy Bypass -Command "$command")
    if ! grep -Eq '"exited"[[:space:]]*:[[:space:]]*1' <<<"$result" \
            || ! grep -Eq '"exitcode"[[:space:]]*:[[:space:]]*0' <<<"$result"; then
        printf 'windows-provision: guest validation failed in VMID %s:\n%s\n' \
            "$vmid" "$result" >&2
        return 1
    fi
}

validate_linked_clone() {
    local vmid name
    vmid=$(pvesh get /cluster/nextid)
    [[ "$vmid" =~ ^[0-9]+$ && "$vmid" != "$WINDOWS_VMID" ]] \
        || fail "PVE returned an invalid validation VMID: $vmid"
    name="proxytools-windows-validation-$vmid"
    ACTIVE_VMID=$vmid

    qm clone "$WINDOWS_VMID" "$vmid" --name "$name" --full 0
    qm set "$vmid" --protection 0
    qm start "$vmid"
    wait_for_agent "$vmid"
    guest_powershell "$vmid" \
        '$ErrorActionPreference="Stop"; if (-not (Test-Path C:\proxytools-template-ready.json)) { throw "ready marker missing" }; $ready=Get-Content C:\proxytools-template-ready.json -Raw | ConvertFrom-Json; if ($ready.template_mode -ne "ready-state-v1") { throw "unexpected template mode" }; if ((Get-Service QEMU-GA).Status -ne "Running") { throw "QGA stopped" }; if ((Get-Service sshd).Status -ne "Running") { throw "sshd stopped" }; if (-not (Test-NetConnection 127.0.0.1 -Port 22 -InformationLevel Quiet)) { throw "sshd not listening" }; if (-not (Test-Path C:\ProgramData\ssh\ssh_host_ed25519_key)) { throw "SSH host key missing" }; $v=& C:\Python313\python.exe --version 2>&1; if ($v -notmatch "^Python 3\.13\.") { throw "bad Python: $v" }; $os=Get-CimInstance Win32_OperatingSystem; if ($os.Caption -notmatch "Windows 11 Enterprise Evaluation") { throw "bad OS: $($os.Caption)" }'
    qm shutdown "$vmid" --timeout 180
    wait_for_stopped "$vmid" 60
    purge_exact_candidate "$vmid" "$name"
    ACTIVE_VMID=
    printf 'Validated Windows linked clone from VMID %s.\n' "$WINDOWS_VMID"
}

build_answer_iso() {
    local password=$1 source target="$IMAGE_DIR/$ANSWER_ISO"
    ANSWER_DIR=$(mktemp -d /tmp/proxytools-windows-answer.XXXXXX)
    for source in autounattend.xml bootstrap.ps1; do
        [[ -f "$SCRIPT_DIR/$source" ]] || fail "required provisioning source is missing: $source"
        cp -- "$SCRIPT_DIR/$source" "$ANSWER_DIR/$source"
    done
    sed -i "s/@@PASSWORD@@/$password/g" "$ANSWER_DIR/autounattend.xml"
    cp -- "$SSH_PUBLIC_KEY" "$ANSWER_DIR/builder.pub"
    cp -- "$IMAGE_DIR/$PYTHON_INSTALLER" "$ANSWER_DIR/$PYTHON_INSTALLER"
    cp -- "$IMAGE_DIR/$OPENSSH_INSTALLER" "$ANSWER_DIR/$OPENSSH_INSTALLER"
    rm -f -- "$target"
    genisoimage -quiet -J -r -V PROXYTOOLS -o "$target" "$ANSWER_DIR"
    printf 'Created temporary unattended-answer ISO: %s\n' "$target"
}

create_candidate() {
    ACTIVE_VMID=$WINDOWS_VMID
    qm create "$WINDOWS_VMID" \
        --name "$WINDOWS_NAME" --description 'Unverified Windows 11 build-template candidate' \
        --ostype win11 --machine q35 --bios ovmf --cpu host --cores 2 \
        --memory "$WINDOWS_MEMORY" --balloon 0 --agent enabled=1 \
        --net0 e1000,bridge=vmbr0,firewall=1 --vga std --onboot 0
    qm set "$WINDOWS_VMID" \
        --efidisk0 local-lvm:1,efitype=4m,pre-enrolled-keys=1 \
        --tpmstate0 local-lvm:4,version=v2.0 \
        --sata0 "local-lvm:$WINDOWS_DISK_GB,discard=on,ssd=1" \
        --ide0 "local:iso/$WINDOWS_ISO,media=cdrom" \
        --ide2 "local:iso/$VIRTIO_ISO,media=cdrom" \
        --sata1 "local:iso/$ANSWER_ISO,media=cdrom"
    # Set boot order only after the referenced disks exist. PVE otherwise
    # silently substitutes its default order and may enter PXE before Setup.
    qm set "$WINDOWS_VMID" --boot 'order=ide0;sata0'
    qm set "$WINDOWS_VMID" --protection 0
    qm start "$WINDOWS_VMID"
    # Microsoft's bootable ISO deliberately requires a key press. The disk is
    # still blank here. Send a short bounded sequence while UEFI discovers the
    # DVD so this never depends on a human PVE console interaction.
    for (( boot_key=1; boot_key<=8; boot_key++ )); do
        sleep 1
        qm sendkey "$WINDOWS_VMID" ret
    done
    printf 'Started unattended Windows installation in VMID %s.\n' "$WINDOWS_VMID"
    # A stopped VM is not proof of a successful installation. Observe the
    # bootstrap marker through QGA before accepting its final clean shutdown.
    wait_for_ready_shutdown "$WINDOWS_VMID" 720
    detach_cached_source_media "$WINDOWS_VMID"
    qm set "$WINDOWS_VMID" --boot 'order=sata0'
    rm -f -- "$IMAGE_DIR/$ANSWER_ISO"
    qm template "$WINDOWS_VMID"
    qm set "$WINDOWS_VMID" --description "$WINDOWS_DESCRIPTION"
    validate_template 0
    validate_linked_clone
    qm set "$WINDOWS_VMID" \
        --description "$WINDOWS_DESCRIPTION" \
        --protection 1
    validate_template 1
    ACTIVE_VMID=
}

main() {
    local password command
    while (( $# )); do
        case "$1" in
            --ssh-public-key)
                (( $# >= 2 )) || fail '--ssh-public-key requires a path'
                SSH_PUBLIC_KEY=$2
                shift 2
                ;;
            --check-only)
                CHECK_ONLY=1
                shift
                ;;
            -h|--help)
                usage
                return
                ;;
            *) fail "unknown argument: $1" ;;
        esac
    done

    (( EUID == 0 )) || fail 'run this script as root on the PVE host'
    for command in awk curl flock genisoimage grep ip od pvesh pvesm qm sed sha256sum tr; do
        command -v "$command" >/dev/null || fail "required command not found: $command"
    done
    pveversion >/dev/null || fail 'this is not a Proxmox VE host'
    pvesm status --storage local | grep -q '^local[[:space:]]' || fail 'local storage unavailable'
    pvesm status --storage local-lvm | grep -q '^local-lvm[[:space:]]' || fail 'local-lvm unavailable'
    ip link show vmbr0 >/dev/null 2>&1 || fail 'vmbr0 unavailable'
    [[ -d "$IMAGE_DIR" ]] || fail "media cache is missing: $IMAGE_DIR"

    if (( CHECK_ONLY )); then
        verify_cached "$WINDOWS_ISO" "$WINDOWS_SHA256"
        verify_cached "$VIRTIO_ISO" "$VIRTIO_SHA256"
        verify_cached "$PYTHON_INSTALLER" "$PYTHON_SHA256"
        verify_cached "$OPENSSH_INSTALLER" "$OPENSSH_SHA256"
        vm_exists "$WINDOWS_VMID" || fail "Windows template is missing: $WINDOWS_VMID"
        validate_template 1
        return
    fi

    [[ -n "$SSH_PUBLIC_KEY" && -f "$SSH_PUBLIC_KEY" ]] \
        || fail '--ssh-public-key must name the dedicated public key'
    trap on_exit EXIT
    exec 9>"$LOCK_FILE"
    flock -n 9 || fail 'another PVE build/provision operation owns the build-lab lock'
    printf 'pid=%s started=%s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&9

    verified_download "$WINDOWS_ISO" "$WINDOWS_URL" "$WINDOWS_SHA256"
    verified_download "$VIRTIO_ISO" "$VIRTIO_URL" "$VIRTIO_SHA256"
    verified_download "$PYTHON_INSTALLER" "$PYTHON_URL" "$PYTHON_SHA256"
    verified_download "$OPENSSH_INSTALLER" "$OPENSSH_URL" "$OPENSSH_SHA256"

    if vm_exists "$WINDOWS_VMID"; then
        if [[ "$(config_value "$WINDOWS_VMID" template)" == 1 ]]; then
            if [[ "$(config_value "$WINDOWS_VMID" protection)" == 1 ]]; then
                validate_template 1
                ACTIVE_VMID=
                return
            fi
            # A ready-state candidate can be resumed after a host-
            # side validation failure. It remains unprotected until a fresh
            # linked clone passes the complete guest contract.
            ACTIVE_VMID=$WINDOWS_VMID
            qm set "$WINDOWS_VMID" --description "$WINDOWS_DESCRIPTION"
            validate_template 0
            validate_linked_clone
            qm set "$WINDOWS_VMID" \
                --description "$WINDOWS_DESCRIPTION" \
                --protection 1
            validate_template 1
            ACTIVE_VMID=
            return
        fi
        [[ "$(config_value "$WINDOWS_VMID" name)" == "$WINDOWS_NAME" ]] \
            || fail "VMID $WINDOWS_VMID is occupied by an unrelated guest"
        purge_exact_candidate "$WINDOWS_VMID" "$WINDOWS_NAME" 1
    fi

    password=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    build_answer_iso "$password"
    create_candidate
    printf 'Windows template provisioning completed.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
