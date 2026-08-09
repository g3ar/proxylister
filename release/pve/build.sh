#!/bin/bash
# Build on the Debian PVE template and validate the artifact on Ubuntu LTS.
#
# Run this script on the development machine from anywhere inside the checkout.
# It transfers the current worktree, including uncommitted changes, into
# disposable linked clones. Publishable release policy is enforced separately.

set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
WORK="$ROOT/release/.work/pve-linux"
BIN="$ROOT/release/bin"
ARTIFACTS="$WORK/artifacts"
LOGS="$WORK/logs"
KNOWN_HOSTS="$WORK/known_hosts"

PVE_HOST=${PROXYTOOLS_PVE_HOST:-root@192.168.66.2}
PVE_ROOT_KEY=${PROXYTOOLS_PVE_ROOT_KEY:-$HOME/.ssh/id_rsa}
GUEST_KEY=${PROXYTOOLS_PVE_GUEST_KEY:-$HOME/.ssh/proxytools-build}
DEBIAN_TEMPLATE=9000
DEBIAN_TEMPLATE_NAME=proxytools-linux-template
UBUNTU_TEMPLATE=9001
UBUNTU_TEMPLATE_NAME=proxytools-ubuntu-2404-check-template

ACTIVE_VMID=
ACTIVE_NAME=
ACTIVE_IP=
ACTIVE_STAGE=

fail() {
    printf 'pve-build: %s\n' "$*" >&2
    exit 1
}

for command in awk git grep rsync sed sha256sum ssh ssh-keygen; do
    command -v "$command" >/dev/null || fail "required command not found: $command"
done
[[ -f "$PVE_ROOT_KEY" ]] || fail "PVE root key not found: $PVE_ROOT_KEY"
[[ -f "$GUEST_KEY" ]] || fail "guest key not found: $GUEST_KEY"

rm -rf -- "$WORK" "$BIN"
mkdir -p -- "$ARTIFACTS" "$LOGS/debian" "$LOGS/ubuntu"
touch "$KNOWN_HOSTS"
chmod 0600 "$KNOWN_HOSTS"

PVE_SSH=(
    ssh -n -F /dev/null -i "$PVE_ROOT_KEY"
    -o BatchMode=yes -o ConnectTimeout=10
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$HOME/.ssh/known_hosts"
)

pve() {
    "${PVE_SSH[@]}" "$PVE_HOST" "$@"
}

guest_rsh() {
    printf 'ssh -F /dev/null -i %s -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=%s' \
        "$GUEST_KEY" "$KNOWN_HOSTS"
}

guest() {
    local ip=$1
    shift
    ssh -F /dev/null -i "$GUEST_KEY" \
        -o BatchMode=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "builder@$ip" "$@"
}

guest_first_contact() {
    local ip=$1
    shift
    ssh -F /dev/null -i "$GUEST_KEY" \
        -o BatchMode=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "builder@$ip" "$@"
}

config_value() {
    local vmid=$1 key=$2
    pve "qm config $vmid" | sed -n "s/^${key}: //p"
}

validate_template() {
    local vmid=$1 expected_name=$2

    [[ "$(pve "qm status $vmid")" == 'status: stopped' ]] \
        || fail "template $vmid must be stopped"
    [[ "$(config_value "$vmid" name)" == "$expected_name" ]] \
        || fail "template $vmid has an unexpected name"
    [[ "$(config_value "$vmid" template)" == 1 ]] \
        || fail "VMID $vmid is not a template"
    [[ "$(config_value "$vmid" protection)" == 1 ]] \
        || fail "template $vmid is not protected"
}

next_vmid() {
    local vmid
    vmid=$(pve 'pvesh get /cluster/nextid')
    [[ "$vmid" =~ ^[0-9]+$ ]] || fail "PVE returned an invalid VMID: $vmid"
    [[ "$vmid" != "$DEBIAN_TEMPLATE" && "$vmid" != "$UBUNTU_TEMPLATE" ]] \
        || fail "PVE returned a protected template VMID: $vmid"
    printf '%s\n' "$vmid"
}

wait_for_agent() {
    local vmid=$1 attempt
    for (( attempt=1; attempt<=180; attempt++ )); do
        if pve "qm agent $vmid ping" >/dev/null 2>&1; then
            return
        fi
        sleep 2
    done
    fail "QEMU Guest Agent did not become ready for clone $vmid"
}

guest_ipv4() {
    local vmid=$1 network ip
    network=$(pve "qm agent $vmid network-get-interfaces")
    ip=$(printf '%s\n' "$network" \
        | sed -n 's/.*"ip-address" : "\([0-9][0-9.]*\)".*/\1/p' \
        | sed '/^127\./d' \
        | sed -n '1p')
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || fail "no non-loopback IPv4 address reported for clone $vmid"
    printf '%s\n' "$ip"
}

wait_for_ssh() {
    local ip=$1 attempt
    for (( attempt=1; attempt<=90; attempt++ )); do
        if guest_first_contact "$ip" true >/dev/null 2>&1; then
            return
        fi
        sleep 2
    done
    fail "SSH did not become ready at $ip"
}

validate_cloud_init() {
    local ip=$1 output status
    set +e
    output=$(guest "$ip" \
        'cloud-init status --wait; status=$?; cloud-init status --long; exit "$status"' \
        2>&1)
    status=$?
    set -e
    printf '%s\n' "$output" >"$LOGS/$ACTIVE_STAGE/cloud-init.log"
    (( status == 0 || status == 2 )) \
        || fail "cloud-init failed in clone $ACTIVE_VMID"
    grep -q '^errors: \[\]$' "$LOGS/$ACTIVE_STAGE/cloud-init.log" \
        || fail "cloud-init reported errors in clone $ACTIVE_VMID"
}

create_clone() {
    local template=$1 name_prefix=$2 stage=$3 vmid
    vmid=$(next_vmid)
    ACTIVE_VMID=$vmid
    ACTIVE_NAME="$name_prefix-$vmid"
    ACTIVE_IP=
    ACTIVE_STAGE=$stage

    pve "qm clone $template $vmid --name $ACTIVE_NAME --full 0"
    pve "qm set $vmid --protection 0"
    pve "qm start $vmid"
    wait_for_agent "$vmid"
    ACTIVE_IP=$(guest_ipv4 "$vmid")
    ssh-keygen -f "$KNOWN_HOSTS" -R "$ACTIVE_IP" >/dev/null 2>&1 || true
    wait_for_ssh "$ACTIVE_IP"
    validate_cloud_init "$ACTIVE_IP"
    printf 'PVE clone %s (%s) is ready at %s.\n' \
        "$ACTIVE_VMID" "$ACTIVE_NAME" "$ACTIVE_IP"
}

retrieve_active_logs() {
    local destination=$1 rsh
    [[ -n "$ACTIVE_IP" ]] || return 0
    rsh=$(guest_rsh)
    mkdir -p -- "$destination"
    rsync -a -e "$rsh" \
        "builder@$ACTIVE_IP:/home/builder/proxytools/release/.work/local-linux/logs/" \
        "$destination/" 2>/dev/null || true
}

on_exit() {
    local status=$?
    if (( status != 0 )); then
        set +e
        if [[ -n "$ACTIVE_VMID" ]]; then
            retrieve_active_logs "$LOGS/failed-$ACTIVE_VMID"
            printf 'pve-build: failed clone retained for diagnosis: VMID=%s name=%s IP=%s\n' \
                "$ACTIVE_VMID" "$ACTIVE_NAME" "${ACTIVE_IP:-unknown}" >&2
            printf 'pve-build: inspect with: ssh %s qm config %s\n' \
                "$PVE_HOST" "$ACTIVE_VMID" >&2
        fi
        printf 'pve-build: diagnostics retained in %s\n' "$WORK" >&2
    fi
}
trap on_exit EXIT

remove_owned_clone() {
    local vmid=$1 expected_name=$2 status attempt

    [[ "$vmid" =~ ^[0-9]+$ ]] || fail "invalid disposable VMID: $vmid"
    [[ "$vmid" != "$DEBIAN_TEMPLATE" && "$vmid" != "$UBUNTU_TEMPLATE" ]] \
        || fail "refusing to delete protected template VMID $vmid"
    [[ "$(config_value "$vmid" name)" == "$expected_name" ]] \
        || fail "clone $vmid has an unexpected name"
    [[ -z "$(config_value "$vmid" template)" ]] \
        || fail "refusing to delete template VMID $vmid"
    [[ "$(config_value "$vmid" protection)" == 0 ]] \
        || fail "refusing to delete protected VMID $vmid"

    status=$(pve "qm status $vmid")
    if [[ "$status" == 'status: running' ]]; then
        pve "qm shutdown $vmid --timeout 120"
    elif [[ "$status" != 'status: stopped' ]]; then
        fail "clone $vmid has unsupported status: $status"
    fi
    for (( attempt=1; attempt<=60; attempt++ )); do
        [[ "$(pve "qm status $vmid")" == 'status: stopped' ]] && break
        sleep 2
    done
    [[ "$(pve "qm status $vmid")" == 'status: stopped' ]] \
        || fail "clone $vmid did not stop"
    pve "qm destroy $vmid --purge 1"
    printf 'Deleted disposable clone %s (%s).\n' "$vmid" "$expected_name"
}

cleanup_stale_clones() {
    local vmid name
    while read -r vmid name; do
        [[ -n "$vmid" && -n "$name" ]] || continue
        case "$name" in
            "proxytools-debian-build-$vmid"|"proxytools-ubuntu-validation-$vmid")
                printf 'Removing stale clone from a previous failed run: %s (%s).\n' \
                    "$vmid" "$name"
                remove_owned_clone "$vmid" "$name"
                ;;
            proxytools-debian-build-*|proxytools-ubuntu-validation-*)
                fail "owned-looking VM $vmid has an unexpected name: $name"
                ;;
        esac
    done < <(pve 'qm list' | awk 'NR > 1 { print $1, $2 }')
}

cleanup_active_clone() {
    [[ -n "$ACTIVE_VMID" ]] || fail 'no active clone to clean up'
    remove_owned_clone "$ACTIVE_VMID" "$ACTIVE_NAME"
    ACTIVE_VMID=
    ACTIVE_NAME=
    ACTIVE_IP=
    ACTIVE_STAGE=
}

transfer_worktree() {
    local ip=$1 rsh
    rsh=$(guest_rsh)
    guest "$ip" 'mkdir -p /home/builder/proxytools'
    rsync -a --delete \
        --exclude=.venv/ \
        --exclude=.git/index.lock \
        --exclude=proxydb/ --exclude=geodb/ \
        --exclude='__pycache__/' --exclude='*.pyc' \
        --exclude=release/.work/ --exclude=release/bin/ \
        -e "$rsh" \
        "$ROOT/" "builder@$ip:/home/builder/proxytools/"
}

run_debian_build() {
    local ip=$1 rsh
    rsh=$(guest_rsh)
    transfer_worktree "$ip"

    if ! guest "$ip" \
            'cd /home/builder/proxytools && ./release/linux/build.sh' \
            >"$LOGS/debian/driver-build.log" 2>&1; then
        tail -n 120 "$LOGS/debian/driver-build.log" >&2
        fail 'Debian build gate failed'
    fi
    if ! guest "$ip" \
            'cd /home/builder/proxytools && ./release/linux/smoke-live.sh release/bin/proxytools' \
            >"$LOGS/debian/driver-live.log" 2>&1; then
        tail -n 120 "$LOGS/debian/driver-live.log" >&2
        fail 'Debian live-smoke gate failed'
    fi

    rsync -a --delete -e "$rsh" \
        "builder@$ip:/home/builder/proxytools/release/bin/" "$ARTIFACTS/"
    rsync -a -e "$rsh" \
        "builder@$ip:/home/builder/proxytools/release/.work/local-linux/logs/" \
        "$LOGS/debian/"
    (cd "$ARTIFACTS" && sha256sum -c SHA256SUMS)
}

run_ubuntu_validation() {
    local ip=$1 rsh
    rsh=$(guest_rsh)
    transfer_worktree "$ip"
    guest "$ip" 'mkdir -p /home/builder/proxytools/release/bin'
    rsync -a --delete -e "$rsh" \
        "$ARTIFACTS/" "builder@$ip:/home/builder/proxytools/release/bin/"

    if ! guest "$ip" \
            'set -eu; cd /home/builder/proxytools/release/bin; sha256sum -c SHA256SUMS; cd /home/builder/proxytools; ./release/linux/smoke.sh release/bin/proxytools; ./release/linux/smoke-live.sh release/bin/proxytools' \
            >"$LOGS/ubuntu/driver-validation.log" 2>&1; then
        tail -n 160 "$LOGS/ubuntu/driver-validation.log" >&2
        fail 'Ubuntu compatibility gate failed'
    fi
    rsync -a -e "$rsh" \
        "builder@$ip:/home/builder/proxytools/release/.work/local-linux/logs/" \
        "$LOGS/ubuntu/"
}

cd "$ROOT"
git rev-parse --show-toplevel >/dev/null
validate_template "$DEBIAN_TEMPLATE" "$DEBIAN_TEMPLATE_NAME"
validate_template "$UBUNTU_TEMPLATE" "$UBUNTU_TEMPLATE_NAME"
cleanup_stale_clones

create_clone "$DEBIAN_TEMPLATE" proxytools-debian-build debian
run_debian_build "$ACTIVE_IP"
cleanup_active_clone

create_clone "$UBUNTU_TEMPLATE" proxytools-ubuntu-validation ubuntu
run_ubuntu_validation "$ACTIVE_IP"
cleanup_active_clone

mv -- "$ARTIFACTS" "$BIN"
trap - EXIT
printf 'PVE Linux artifact: %s\n' "$BIN/proxytools"
printf 'PVE logs: %s\n' "$LOGS"
