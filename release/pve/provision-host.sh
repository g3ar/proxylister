#!/bin/bash
# Provision the reusable Proxy Tools Linux templates on a clean Proxmox VE host.
#
# Run this script as root on the PVE host after its storage and vmbr0 network
# have been configured by the PVE installer. It validates host prerequisites,
# verifies official cloud images, bakes required guest packages, and creates
# protected templates. Existing valid templates are never rebuilt or modified.

set -euo pipefail

DEBIAN_VMID=9000
DEBIAN_NAME=proxytools-linux-template
UBUNTU_VMID=9001
UBUNTU_NAME=proxytools-ubuntu-2404-check-template
IMAGE_DIR=/var/lib/vz/template/iso
SNIPPET_DIR=/var/lib/vz/snippets
SSH_PUBLIC_KEY=
CHECK_ONLY=0
ACTIVE_VMID=
LOCK_FILE=/run/lock/proxytools-pve-build.lock

usage() {
    cat <<'EOF'
Usage:
  ./provision-host.sh --ssh-public-key /path/to/proxytools-build.pub
  ./provision-host.sh --check-only

The normal mode creates missing templates 9000 and 9001. Existing valid
templates are only checked. Any occupied or partial VMID causes a hard failure;
the script never replaces a VM, template, ISO, or cloud image automatically.

Host prerequisites managed outside this script:
  - a working Proxmox VE installation;
  - local directory storage with iso/snippets/import content;
  - local-lvm storage for VM images;
  - vmbr0 with DHCP access for guests.
EOF
}

fail() {
    printf 'provision-host: %s\n' "$*" >&2
    exit 1
}

on_exit() {
    status=$?
    if (( status != 0 )) && [[ -n "$ACTIVE_VMID" ]]; then
        printf 'provision-host: failed while provisioning VMID %s; it was left intact for diagnosis.\n' \
            "$ACTIVE_VMID" >&2
        printf 'provision-host: inspect with: qm status %s; qm config %s\n' \
            "$ACTIVE_VMID" "$ACTIVE_VMID" >&2
    fi
}
trap on_exit EXIT

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
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

(( EUID == 0 )) || fail 'run this script as root on the PVE host'

for command in curl flock ip lvmconfig pvesh pvesm qm qemu-img sha256sum sha512sum; do
    command -v "$command" >/dev/null || fail "required command not found: $command"
done

exec 9>"$LOCK_FILE"
flock -n 9 \
    || fail 'another PVE build/provision operation already owns the build-lab lock'
printf 'pid=%s started=%s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&9

configure_thin_pool_autoextend() {
    local threshold percent backup

    threshold=$(lvmconfig --type full activation/thin_pool_autoextend_threshold 9>&- \
        | sed 's/.*=//')
    percent=$(lvmconfig --type full activation/thin_pool_autoextend_percent 9>&- \
        | sed 's/.*=//')
    if [[ "$threshold" == 80 && "$percent" == 20 ]]; then
        return
    fi

    backup="/etc/lvm/lvm.conf.proxytools.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -a /etc/lvm/lvm.conf "$backup"
    sed -i '/^activation {/a\
\tthin_pool_autoextend_threshold = 80\
\tthin_pool_autoextend_percent = 20' /etc/lvm/lvm.conf
    if ! lvmconfig --validate 9>&-; then
        cp -a "$backup" /etc/lvm/lvm.conf
        fail 'invalid LVM configuration; restored the original file'
    fi
    systemctl restart lvm2-monitor.service
    printf 'Configured LVM thin-pool autoextend (80%% threshold, 20%% growth); backup: %s\n' \
        "$backup"
}

configure_thin_pool_autoextend

pveversion >/dev/null || fail 'this does not appear to be a Proxmox VE host'
pvesm status --storage local | grep -q '^local[[:space:]]' \
    || fail 'required storage is unavailable: local'
pvesm status --storage local-lvm | grep -q '^local-lvm[[:space:]]' \
    || fail 'required storage is unavailable: local-lvm'
grep -A5 '^dir: local$' /etc/pve/storage.cfg | grep -q 'snippets' \
    || fail 'local storage must allow snippets content'
ip link show vmbr0 >/dev/null 2>&1 || fail 'required guest bridge is unavailable: vmbr0'
mkdir -p -- "$IMAGE_DIR" "$SNIPPET_DIR"

config_value() {
    local vmid=$1 key=$2
    qm config "$vmid" | sed -n "s/^${key}: //p"
}

validate_template() {
    local vmid=$1 expected_name=$2 expected_memory=$3

    [[ "$(qm status "$vmid")" == 'status: stopped' ]] \
        || fail "VMID $vmid must be stopped"
    [[ "$(config_value "$vmid" name)" == "$expected_name" ]] \
        || fail "VMID $vmid has an unexpected name"
    [[ "$(config_value "$vmid" template)" == 1 ]] \
        || fail "VMID $vmid is not a template"
    [[ "$(config_value "$vmid" protection)" == 1 ]] \
        || fail "VMID $vmid is not protected"
    [[ "$(config_value "$vmid" cores)" == 2 ]] \
        || fail "VMID $vmid must have 2 cores"
    [[ "$(config_value "$vmid" memory)" == "$expected_memory" ]] \
        || fail "VMID $vmid has unexpected memory"
    [[ "$(config_value "$vmid" ciuser)" == builder ]] \
        || fail "VMID $vmid must use cloud-init user builder"
    [[ "$(config_value "$vmid" ipconfig0)" == 'ip=dhcp' ]] \
        || fail "VMID $vmid must use DHCP"
    qm config "$vmid" | grep -q '^agent: enabled=1' \
        || fail "VMID $vmid must enable QEMU Guest Agent"
    qm config "$vmid" | grep -q '^scsi0: local-lvm:base-' \
        || fail "VMID $vmid must have a base disk on local-lvm"
    printf 'Validated template %s (%s).\n' "$vmid" "$expected_name"
}

template_exists() {
    test -e "/etc/pve/qemu-server/$1.conf"
}

checksum_for() {
    local sums=$1 filename=$2
    awk -v wanted="$filename" '
        $2 == wanted || $2 == "*" wanted { print $1; found=1 }
        END { if (!found) exit 1 }
    ' "$sums"
}

verified_image() {
    local url=$1 sums_url=$2 algorithm=$3 filename=$4
    local target="$IMAGE_DIR/$filename"
    local sums temporary expected actual

    sums=$(mktemp "$IMAGE_DIR/.proxytools-sums.XXXXXX")
    curl -fL --retry 5 --output "$sums" "$sums_url" \
        || fail "could not download official checksum list: $sums_url"
    expected=$(checksum_for "$sums" "$filename") \
        || fail "official checksum list does not contain $filename"

    if [[ -e "$target" ]]; then
        actual=$("${algorithm}sum" "$target" | awk '{print $1}')
        rm -f -- "$sums"
        [[ "$actual" == "$expected" ]] \
            || fail "existing image failed its current official checksum: $target"
        printf '%s\n' "$target"
        return
    fi

    temporary="$target.download.$$"
    curl -fL --retry 5 --output "$temporary" "$url" \
        || fail "could not download cloud image; partial file retained as $temporary"
    actual=$("${algorithm}sum" "$temporary" | awk '{print $1}')
    rm -f -- "$sums"
    if [[ "$actual" != "$expected" ]]; then
        fail "downloaded image failed its official checksum; retained as $temporary"
    fi
    mv -- "$temporary" "$target"
    printf '%s\n' "$target"
}

wait_for_agent() {
    local vmid=$1 attempt
    # The Debian package upgrade can be slow on the low-power build host.
    for (( attempt=1; attempt<=900; attempt++ )); do
        if qm agent "$vmid" ping >/dev/null 2>&1; then
            return
        fi
        sleep 2
    done
    fail "QEMU Guest Agent did not become ready for VMID $vmid"
}

wait_for_stopped() {
    local vmid=$1 attempt
    for (( attempt=1; attempt<=90; attempt++ )); do
        if [[ "$(qm status "$vmid")" == 'status: stopped' ]]; then
            return
        fi
        sleep 2
    done
    fail "VMID $vmid did not stop within the bounded wait"
}

guest_shell() {
    local vmid=$1 script=$2
    local result

    result=$(qm guest exec "$vmid" -- /bin/sh -c "$script")
    if ! grep -Eq '"exited"[[:space:]]*:[[:space:]]*1' <<<"$result" \
            || ! grep -Eq '"exitcode"[[:space:]]*:[[:space:]]*0' <<<"$result"; then
        printf 'provision-host: guest command failed in VMID %s:\n%s\n' \
            "$vmid" "$result" >&2
        return 1
    fi
}

validate_linked_clone() {
    local template_vmid=$1 verify=$2
    local clone_vmid clone_name

    clone_vmid=$(pvesh get /cluster/nextid)
    [[ "$clone_vmid" != "$DEBIAN_VMID" && "$clone_vmid" != "$UBUNTU_VMID" ]] \
        || fail "PVE returned a template VMID for validation: $clone_vmid"
    clone_name="proxytools-bootstrap-validation-$clone_vmid"
    ACTIVE_VMID=$clone_vmid

    qm clone "$template_vmid" "$clone_vmid" --name "$clone_name" --full 0
    qm set "$clone_vmid" --protection 0
    qm start "$clone_vmid"
    wait_for_agent "$clone_vmid"
    guest_shell "$clone_vmid" \
        'cloud-init status --wait >/dev/null 2>&1 || test "$?" -eq 2; cloud-init status --long | grep -q "^errors: \[\]$"'
    guest_shell "$clone_vmid" "$verify"
    qm shutdown "$clone_vmid" --timeout 120
    wait_for_stopped "$clone_vmid"

    [[ "$(config_value "$clone_vmid" name)" == "$clone_name" ]] \
        || fail "validation clone $clone_vmid has an unexpected name"
    [[ -z "$(config_value "$clone_vmid" template)" ]] \
        || fail "refusing to delete template VMID $clone_vmid"
    [[ "$(config_value "$clone_vmid" protection)" == 0 ]] \
        || fail "validation clone $clone_vmid is protected"
    qm destroy "$clone_vmid" --purge 1
    ACTIVE_VMID=
    printf 'Validated linked-clone boot from template %s.\n' "$template_vmid"
}

create_template() {
    local vmid=$1 name=$2 memory=$3 description=$4 image=$5 vendor=$6 verify=$7
    local key_copy="$SNIPPET_DIR/.proxytools-key-$vmid.pub"
    local vendor_path="$SNIPPET_DIR/proxytools-provision-$vmid.yaml"

    template_exists "$vmid" && fail "VMID $vmid already exists and was not validated"
    [[ -n "$SSH_PUBLIC_KEY" ]] || fail '--ssh-public-key is required to create templates'
    [[ -f "$SSH_PUBLIC_KEY" ]] || fail "SSH public key not found: $SSH_PUBLIC_KEY"

    ACTIVE_VMID=$vmid
    install -m 0600 -- "$SSH_PUBLIC_KEY" "$key_copy"
    install -m 0644 -- "$vendor" "$vendor_path"

    qm create "$vmid" \
        --name "$name" --description "$description" --ostype l26 \
        --cpu host --cores 2 --memory "$memory" --balloon 0 \
        --scsihw virtio-scsi-single \
        --net0 virtio,bridge=vmbr0,firewall=1 \
        --agent enabled=1,fstrim_cloned_disks=1 \
        --serial0 socket --vga serial0 --onboot 0
    qm importdisk "$vmid" "$image" local-lvm
    qm set "$vmid" \
        --scsi0 "local-lvm:vm-$vmid-disk-0,discard=on,iothread=1,ssd=1" \
        --boot order=scsi0 --ide2 local-lvm:cloudinit \
        --ciuser builder --sshkeys "$key_copy" --ipconfig0 ip=dhcp \
        --ciupgrade 0 --cicustom "vendor=local:snippets/$(basename "$vendor_path")"
    qm resize "$vmid" scsi0 20G
    rm -f -- "$key_copy"

    qm start "$vmid"
    wait_for_agent "$vmid"
    guest_shell "$vmid" \
        'cloud-init status --wait >/dev/null 2>&1 || test "$?" -eq 2; cloud-init status --long | grep -q "^errors: \[\]$"'
    guest_shell "$vmid" "$verify"
    guest_shell "$vmid" \
        'cloud-init clean --logs --machine-id && rm -f /etc/ssh/ssh_host_* && sync'

    qm shutdown "$vmid" --timeout 120
    wait_for_stopped "$vmid"
    qm set "$vmid" --delete cicustom
    rm -f -- "$vendor_path"
    qm template "$vmid"
    qm set "$vmid" --protection 1
    validate_template "$vmid" "$name" "$memory"
    ACTIVE_VMID=
    validate_linked_clone "$vmid" "$verify"
}

if template_exists "$DEBIAN_VMID"; then
    validate_template "$DEBIAN_VMID" "$DEBIAN_NAME" 3072
elif (( CHECK_ONLY )); then
    fail "required template is missing: $DEBIAN_VMID"
else
    debian_image=$(verified_image \
        'https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2' \
        'https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS' \
        sha512 'debian-13-genericcloud-amd64.qcow2')
    debian_vendor=$(mktemp /tmp/proxytools-debian-vendor.XXXXXX)
    cat >"$debian_vendor" <<'EOF'
#cloud-config
package_update: true
package_upgrade: true
packages:
  - python3
  - python3-venv
  - python3-dev
  - build-essential
  - git
  - rsync
  - openssh-server
  - ca-certificates
  - curl
  - file
  - qemu-guest-agent
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent.service]
EOF
    create_template "$DEBIAN_VMID" "$DEBIAN_NAME" 3072 \
        'Debian 13 stable immutable builder template' \
        "$debian_image" "$debian_vendor" \
        'test -f /etc/debian_version && command -v python3 git rsync cc curl qemu-ga >/dev/null'
    rm -f -- "$debian_vendor"
fi

if template_exists "$UBUNTU_VMID"; then
    validate_template "$UBUNTU_VMID" "$UBUNTU_NAME" 2048
elif (( CHECK_ONLY )); then
    fail "required template is missing: $UBUNTU_VMID"
else
    ubuntu_image=$(verified_image \
        'https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img' \
        'https://cloud-images.ubuntu.com/releases/noble/release/SHA256SUMS' \
        sha256 'ubuntu-24.04-server-cloudimg-amd64.img')
    ubuntu_vendor=$(mktemp /tmp/proxytools-ubuntu-vendor.XXXXXX)
    cat >"$ubuntu_vendor" <<'EOF'
#cloud-config
package_update: true
packages:
  - qemu-guest-agent
  - ca-certificates
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent.service]
EOF
    create_template "$UBUNTU_VMID" "$UBUNTU_NAME" 2048 \
        'Ubuntu 24.04 LTS immutable binary compatibility-check template' \
        "$ubuntu_image" "$ubuntu_vendor" \
        'grep -q "^ID=ubuntu$" /etc/os-release && command -v qemu-ga >/dev/null'
    rm -f -- "$ubuntu_vendor"
fi

printf 'PVE build-lab provisioning is complete.\n'
