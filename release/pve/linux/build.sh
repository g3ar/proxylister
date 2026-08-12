#!/bin/bash
# Build on the Debian PVE template and validate the artifact on Ubuntu LTS.
#
# Run this script on the development machine from anywhere inside the checkout.
# It transfers the current worktree, including uncommitted changes, into
# disposable linked clones. Publishable release policy is enforced separately.

set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
WORK="$ROOT/release/.work/pve-linux"
BIN="$ROOT/release/bin"
ARTIFACTS="$WORK/artifacts"
LOGS="$WORK/logs"
KNOWN_HOSTS="$WORK/known_hosts"
SOURCE_ARCHIVE="$WORK/source.tar"
SOURCE_CHECKSUM="$WORK/source.tar.sha256"

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
PVE_LOCK_PID=
PVE_LOCK_FD=
RUN_STARTED=0
RELEASE_MODE=0
SOURCE_COMMIT=
SOURCE_TREE=

fail() {
    printf 'pve-build: %s\n' "$*" >&2
    exit 1
}

PVE_SSH=(
    ssh -n -F /dev/null -i "$PVE_ROOT_KEY"
    -o BatchMode=yes -o ConnectTimeout=10
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$HOME/.ssh/known_hosts"
)

PVE_LOCK_SSH=(
    ssh -F /dev/null -i "$PVE_ROOT_KEY"
    -o BatchMode=yes -o ConnectTimeout=10
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$HOME/.ssh/known_hosts"
)

pve() {
    "${PVE_SSH[@]}" "$PVE_HOST" "$@"
}

acquire_pve_lock() {
    local response

    coproc PVE_LOCK_PROCESS {
        "${PVE_LOCK_SSH[@]}" "$PVE_HOST" \
            'exec 9>/run/lock/proxytools-pve-build.lock; flock -n 9 || exit 73; printf "pid=%s started=%s\n" "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&9; printf "LOCKED\n"; cat >/dev/null'
    }
    PVE_LOCK_PID=$PVE_LOCK_PROCESS_PID
    if ! read -r -t 15 response <&"${PVE_LOCK_PROCESS[0]}" \
            || [[ "$response" != LOCKED ]]; then
        wait "$PVE_LOCK_PID" 2>/dev/null || true
        PVE_LOCK_PID=
        fail 'another PVE build/provision operation already owns the build-lab lock'
    fi
    exec {PVE_LOCK_FD}>&"${PVE_LOCK_PROCESS[1]}"
    exec {PVE_LOCK_PROCESS[0]}>&-
    exec {PVE_LOCK_PROCESS[1]}>&-
    printf 'Acquired PVE build-lab lock.\n'
}

release_pve_lock() {
    [[ -n "$PVE_LOCK_FD" ]] || return 0
    exec {PVE_LOCK_FD}>&-
    wait "$PVE_LOCK_PID" 2>/dev/null || true
    PVE_LOCK_FD=
    PVE_LOCK_PID=
    printf 'Released PVE build-lab lock.\n'
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

references_cached_source_media() {
    local line=$1
    [[ "$line" == *':iso/'* \
        || "$line" == *'/var/lib/vz/template/iso/'* ]]
}

detach_cached_source_media() {
    local vmid=$1 config line slot

    config=$(pve "qm config $vmid")
    while IFS= read -r line; do
        references_cached_source_media "$line" || continue
        slot=${line%%:*}
        if [[ "$slot" =~ ^(ide|sata|scsi|virtio|unused)[0-9]+$ ]]; then
            pve "qm set $vmid --delete $slot"
            printf 'Detached cached source media from clone %s slot %s.\n' \
                "$vmid" "$slot"
        fi
    done <<<"$config"

    config=$(pve "qm config $vmid")
    while IFS= read -r line; do
        references_cached_source_media "$line" || continue
        fail "refusing to delete clone $vmid while its config references cached source media: $line"
    done <<<"$config"
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
    if (( status != 0 && RUN_STARTED )); then
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
    release_pve_lock
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
    detach_cached_source_media "$vmid"
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

prepare_release_source() {
    [[ -z "$(git status --porcelain)" ]] \
        || fail 'release mode requires a clean worktree, including no untracked files'
    git archive --format=tar --output="$SOURCE_ARCHIVE" HEAD
    (
        cd "$WORK"
        sha256sum "$(basename "$SOURCE_ARCHIVE")" >"$(basename "$SOURCE_CHECKSUM")"
    )
}

transfer_source() {
    local ip=$1 rsh
    if (( ! RELEASE_MODE )); then
        transfer_worktree "$ip"
        return
    fi

    rsh=$(guest_rsh)
    guest "$ip" 'rm -rf /home/builder/proxytools && mkdir -p /home/builder/proxytools'
    rsync -a -e "$rsh" "$SOURCE_ARCHIVE" "$SOURCE_CHECKSUM" \
        "builder@$ip:/home/builder/"
    guest "$ip" \
        'set -eu; cd /home/builder; sha256sum -c source.tar.sha256; tar -xf source.tar -C proxytools'
}

run_debian_build() {
    local ip=$1 rsh
    rsh=$(guest_rsh)
    transfer_source "$ip"

    if ! guest "$ip" \
            "cd /home/builder/proxytools && PROXYTOOLS_SOURCE_COMMIT=$SOURCE_COMMIT PROXYTOOLS_SOURCE_TREE=$SOURCE_TREE ./release/linux/build.sh" \
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
    transfer_source "$ip"
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

main() {
    local command

    while (( $# )); do
        case "$1" in
            --release)
                RELEASE_MODE=1
                shift
                ;;
            -h|--help)
                printf 'Usage: %s [--release]\n' "$0"
                return
                ;;
            *)
                fail "unknown argument: $1"
                ;;
        esac
    done

    for command in awk git grep rsync sed sha256sum ssh ssh-keygen; do
        command -v "$command" >/dev/null \
            || fail "required command not found: $command"
    done
    [[ -f "$PVE_ROOT_KEY" ]] || fail "PVE root key not found: $PVE_ROOT_KEY"
    [[ -f "$GUEST_KEY" ]] || fail "guest key not found: $GUEST_KEY"

    cd "$ROOT"
    git rev-parse --show-toplevel >/dev/null
    SOURCE_COMMIT=$(git rev-parse HEAD)
    if [[ -z "$(git status --porcelain)" ]]; then
        SOURCE_TREE=clean
    else
        SOURCE_TREE=dirty
    fi
    acquire_pve_lock
    if (( RELEASE_MODE )); then
        [[ "$SOURCE_TREE" == clean ]] \
            || fail 'release mode requires a clean worktree, including no untracked files'
    fi
    rm -rf -- "$WORK" "$BIN"
    mkdir -p -- "$ARTIFACTS" "$LOGS/debian" "$LOGS/ubuntu"
    touch "$KNOWN_HOSTS"
    chmod 0600 "$KNOWN_HOSTS"
    if (( RELEASE_MODE )); then
        prepare_release_source
    fi
    RUN_STARTED=1
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
    release_pve_lock
    trap - EXIT
    printf 'PVE Linux artifact: %s\n' "$BIN/proxytools"
    printf 'PVE logs: %s\n' "$LOGS"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
