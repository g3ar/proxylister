#!/bin/bash
# Build and test proxytools.exe in a disposable linked clone of template 9002.
# Run this driver on the development machine from anywhere in the checkout.

set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
WORK="$ROOT/release/.work/pve-windows"
BIN_ROOT="$ROOT/release/bin"
BIN="$BIN_ROOT/windows"
ARTIFACTS="$WORK/artifacts"
LOGS="$WORK/logs"
KNOWN_HOSTS="$WORK/known_hosts"
SOURCE_ARCHIVE="$WORK/source.tar"
SOURCE_CHECKSUM="$WORK/source.tar.sha256"

PVE_HOST=${PROXYTOOLS_PVE_HOST:-root@192.168.66.2}
PVE_ROOT_KEY=${PROXYTOOLS_PVE_ROOT_KEY:-$HOME/.ssh/id_rsa}
GUEST_KEY=${PROXYTOOLS_PVE_GUEST_KEY:-$HOME/.ssh/proxytools-build}
WINDOWS_TEMPLATE=9002
WINDOWS_TEMPLATE_NAME=proxytools-windows-template

ACTIVE_VMID=
ACTIVE_NAME=
ACTIVE_IP=
PVE_LOCK_PID=
PVE_LOCK_FD=
RUN_STARTED=0
RELEASE_MODE=0
SOURCE_COMMIT=
SOURCE_TREE=

fail() {
    printf 'pve-windows-build: %s\n' "$*" >&2
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

guest_scp() {
    local ip=$1 source=$2 destination=$3
    # Win32-OpenSSH 10.0 ships scp.exe in the template. Force the legacy SCP
    # protocol because current Linux scp defaults to SFTP, whose relative
    # subsystem path is not resolved by this Windows service installation.
    scp -O -F /dev/null -i "$GUEST_KEY" \
        -o BatchMode=yes -o ConnectTimeout=20 \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "$source" "builder@$ip:$destination"
}

guest_scp_from() {
    local ip=$1 source=$2 destination=$3
    scp -O -F /dev/null -i "$GUEST_KEY" \
        -o BatchMode=yes -o ConnectTimeout=20 \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "builder@$ip:$source" "$destination"
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
        fail 'another PVE build/provision operation owns the build-lab lock'
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

config_value() {
    local vmid=$1 key=$2
    pve "qm config $vmid" | sed -n "s/^${key}: //p"
}

references_cached_source_media() {
    local line=$1
    [[ "$line" == *':iso/'* || "$line" == *'/var/lib/vz/template/iso/'* ]]
}

detach_cached_source_media() {
    local vmid=$1 config line slot
    config=$(pve "qm config $vmid")
    while IFS= read -r line; do
        references_cached_source_media "$line" || continue
        slot=${line%%:*}
        if [[ "$slot" =~ ^(ide|sata|scsi|virtio|unused)[0-9]+$ ]]; then
            pve "qm set $vmid --delete $slot"
            printf 'Detached cached source media from clone %s slot %s.\n' "$vmid" "$slot"
        fi
    done <<<"$config"
    config=$(pve "qm config $vmid")
    while IFS= read -r line; do
        references_cached_source_media "$line" || continue
        fail "refusing to delete clone $vmid while cached source media remains attached: $line"
    done <<<"$config"
}

validate_template() {
    [[ "$(pve "qm status $WINDOWS_TEMPLATE")" == 'status: stopped' ]] \
        || fail "template $WINDOWS_TEMPLATE must be stopped"
    [[ "$(config_value "$WINDOWS_TEMPLATE" name)" == "$WINDOWS_TEMPLATE_NAME" ]] \
        || fail "template $WINDOWS_TEMPLATE has an unexpected name"
    [[ "$(config_value "$WINDOWS_TEMPLATE" template)" == 1 ]] \
        || fail "VMID $WINDOWS_TEMPLATE is not a template"
    [[ "$(config_value "$WINDOWS_TEMPLATE" protection)" == 1 ]] \
        || fail "template $WINDOWS_TEMPLATE is not protected"
}

next_vmid() {
    local vmid
    vmid=$(pve 'pvesh get /cluster/nextid')
    [[ "$vmid" =~ ^[0-9]+$ && "$vmid" != "$WINDOWS_TEMPLATE" ]] \
        || fail "PVE returned an invalid VMID: $vmid"
    printf '%s\n' "$vmid"
}

wait_for_agent() {
    local vmid=$1 attempt
    for (( attempt=1; attempt<=360; attempt++ )); do
        pve "qm agent $vmid ping" >/dev/null 2>&1 && return
        sleep 2
    done
    fail "QEMU Guest Agent did not become ready for clone $vmid"
}

guest_ipv4() {
    local vmid=$1 network ip
    network=$(pve "qm agent $vmid network-get-interfaces")
    ip=$(printf '%s\n' "$network" \
        | sed -n 's/.*"ip-address" : "\([0-9][0-9.]*\)".*/\1/p' \
        | sed '/^127\./d' | sed -n '1p')
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || fail "no non-loopback IPv4 reported for clone $vmid"
    printf '%s\n' "$ip"
}

wait_for_ssh() {
    local ip=$1 attempt
    for (( attempt=1; attempt<=180; attempt++ )); do
        if guest_first_contact "$ip" 'cmd.exe /d /c exit 0' >/dev/null 2>&1; then
            return
        fi
        sleep 2
    done
    fail "SSH did not become ready at $ip"
}

validate_guest() {
    local ip=$1
    guest "$ip" "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"\$ErrorActionPreference='Stop'; if (-not (Test-Path C:\proxytools-template-ready.json)) { throw 'ready marker missing' }; \$ready=Get-Content C:\proxytools-template-ready.json -Raw | ConvertFrom-Json; if (\$ready.template_mode -ne 'ready-state-v1') { throw 'unexpected template mode' }; if ((Get-Service QEMU-GA).Status -ne 'Running') { throw 'QGA stopped' }; if ((Get-Service sshd).Status -ne 'Running') { throw 'sshd stopped' }; \$v=& C:\Python313\python.exe --version 2>&1; if (\$v -notmatch '^Python 3\.13\.') { throw 'unexpected Python' }\""
}

activate_guest() {
    local ip=$1
    # Enterprise Evaluation needs normal online activation even though its
    # official media does not require a product key. The prepared template is
    # already activated; keep this gate because Microsoft may require a cloned
    # virtual machine to activate again before a longer build run.
    guest "$ip" "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"\$ErrorActionPreference='Stop'; function Get-EvaluationLicense { Get-CimInstance SoftwareLicensingProduct | Where-Object { \$_.PartialProductKey -and \$_.Description -match 'TIMEBASED_EVAL' } | Select-Object -First 1 }; \$license=Get-EvaluationLicense; if (-not \$license) { throw 'Windows Evaluation license not found' }; if (\$license.LicenseStatus -ne 1 -or \$license.GracePeriodRemaining -le 0) { \$output=& cscript.exe //Nologo C:\Windows\System32\slmgr.vbs /ato 2>&1; if (\$LASTEXITCODE -ne 0) { throw ('Windows Evaluation activation failed: {0}' -f (\$output -join ' ')) }; Start-Sleep -Seconds 5; \$license=Get-EvaluationLicense }; if (\$license.LicenseStatus -ne 1 -or \$license.GracePeriodRemaining -le 0) { throw ('Windows Evaluation is not licensed: status={0} grace={1}' -f \$license.LicenseStatus, \$license.GracePeriodRemaining) }; Write-Output ('Windows Evaluation licensed; grace minutes: {0}' -f \$license.GracePeriodRemaining)\""
}

create_clone() {
    local vmid
    vmid=$(next_vmid)
    ACTIVE_VMID=$vmid
    ACTIVE_NAME="proxytools-windows-build-$vmid"
    ACTIVE_IP=
    pve "qm clone $WINDOWS_TEMPLATE $vmid --name $ACTIVE_NAME --full 0"
    pve "qm set $vmid --protection 0"
    pve "qm start $vmid"
    wait_for_agent "$vmid"
    ACTIVE_IP=$(guest_ipv4 "$vmid")
    ssh-keygen -f "$KNOWN_HOSTS" -R "$ACTIVE_IP" >/dev/null 2>&1 || true
    wait_for_ssh "$ACTIVE_IP"
    validate_guest "$ACTIVE_IP"
    activate_guest "$ACTIVE_IP"
    printf 'Windows clone %s (%s) is ready at %s.\n' "$ACTIVE_VMID" "$ACTIVE_NAME" "$ACTIVE_IP"
}

stop_clone() {
    local vmid=$1 status attempt
    status=$(pve "qm status $vmid")
    if [[ "$status" == 'status: running' ]]; then
        pve "qm shutdown $vmid --timeout 180" || pve "qm stop $vmid --overrule-shutdown 1"
    elif [[ "$status" != 'status: stopped' ]]; then
        fail "clone $vmid has unsupported status: $status"
    fi
    for (( attempt=1; attempt<=90; attempt++ )); do
        [[ "$(pve "qm status $vmid")" == 'status: stopped' ]] && return
        sleep 2
    done
    fail "clone $vmid did not stop"
}

remove_owned_clone() {
    local vmid=$1 expected_name=$2
    [[ "$vmid" =~ ^[0-9]+$ && "$vmid" != "$WINDOWS_TEMPLATE" ]] \
        || fail "refusing to delete Windows template VMID $vmid"
    [[ "$(config_value "$vmid" name)" == "$expected_name" ]] \
        || fail "clone $vmid has an unexpected name"
    [[ -z "$(config_value "$vmid" template)" ]] \
        || fail "refusing to delete template VMID $vmid"
    [[ "$(config_value "$vmid" protection)" == 0 ]] \
        || fail "refusing to delete protected VMID $vmid"
    stop_clone "$vmid"
    detach_cached_source_media "$vmid"
    pve "qm destroy $vmid --purge 1"
    printf 'Deleted disposable Windows clone %s (%s).\n' "$vmid" "$expected_name"
}

cleanup_stale_clones() {
    local vmid name
    while read -r vmid name; do
        [[ -n "$vmid" && -n "$name" ]] || continue
        case "$name" in
            "proxytools-windows-build-$vmid")
                printf 'Removing stale Windows clone: %s (%s).\n' "$vmid" "$name"
                remove_owned_clone "$vmid" "$name"
                ;;
            proxytools-windows-build-*)
                fail "owned-looking VM $vmid has an unexpected name: $name"
                ;;
        esac
    done < <(pve 'qm list' | awk 'NR > 1 { print $1, $2 }')
}

cleanup_active_clone() {
    [[ -n "$ACTIVE_VMID" ]] || fail 'no active Windows clone to clean up'
    remove_owned_clone "$ACTIVE_VMID" "$ACTIVE_NAME"
    ACTIVE_VMID=
    ACTIVE_NAME=
    ACTIVE_IP=
}

prepare_source() {
    if (( RELEASE_MODE )); then
        [[ -z "$(git status --porcelain)" ]] \
            || fail 'release mode requires a clean worktree, including no untracked files'
        git archive --format=tar --output="$SOURCE_ARCHIVE" HEAD
    else
        tar --exclude='./.git' --exclude='./.venv' \
            --exclude='./proxydb' --exclude='./geodb' \
            --exclude='./release/.work' --exclude='./release/bin' \
            --exclude='__pycache__' --exclude='*.pyc' \
            -cf "$SOURCE_ARCHIVE" -C "$ROOT" .
    fi
    (cd "$WORK" && sha256sum "$(basename "$SOURCE_ARCHIVE")" >"$(basename "$SOURCE_CHECKSUM")")
}

transfer_source() {
    local ip=$1
    guest "$ip" 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Remove-Item -LiteralPath C:\Users\builder\proxytools-build -Recurse -Force -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Path C:\Users\builder\proxytools-build\source -Force | Out-Null"'
    guest_scp "$ip" "$SOURCE_ARCHIVE" 'proxytools-build/source.tar'
    guest_scp "$ip" "$SOURCE_CHECKSUM" 'proxytools-build/source.tar.sha256'
    guest "$ip" "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"\$ErrorActionPreference='Stop'; \$root='C:\Users\builder\proxytools-build'; \$expected=((Get-Content -LiteralPath (\$root+'\source.tar.sha256')) -split '\s+')[0]; \$actual=(Get-FileHash -LiteralPath (\$root+'\source.tar') -Algorithm SHA256).Hash.ToLowerInvariant(); if (\$actual -ne \$expected) { throw 'source archive checksum mismatch' }; tar.exe -xf (\$root+'\source.tar') -C (\$root+'\source'); if (\$LASTEXITCODE -ne 0) { throw 'source extraction failed' }\""
}

retrieve_logs() {
    local destination=$1 file
    [[ -n "$ACTIVE_IP" ]] || return 0
    mkdir -p -- "$destination"
    for file in build.log smoke.log live-list.log live-list-error.log; do
        guest_scp_from "$ACTIVE_IP" \
            "proxytools-build/source/release/.work/windows/logs/$file" \
            "$destination/$file" 2>/dev/null || true
    done
}

run_windows_build() {
    local ip=$1 file
    transfer_source "$ip"
    if ! guest "$ip" \
            "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\\Users\\builder\\proxytools-build\\source\\release\\pve\\windows\\build.ps1 -SourceCommit $SOURCE_COMMIT -SourceTree $SOURCE_TREE" \
            >"$LOGS/driver-build.log" 2>&1; then
        tail -n 160 "$LOGS/driver-build.log" >&2
        fail 'Windows build gate failed'
    fi
    if ! guest "$ip" \
            'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\Users\builder\proxytools-build\source\release\pve\windows\smoke-live.ps1 -Artifact C:\Users\builder\proxytools-build\source\release\bin\windows\proxytools.exe' \
            >"$LOGS/driver-live.log" 2>&1; then
        tail -n 160 "$LOGS/driver-live.log" >&2
        fail 'Windows live-smoke gate failed'
    fi
    mkdir -p -- "$ARTIFACTS"
    for file in proxytools.exe README.md LICENSE MANIFEST.txt SHA256SUMS; do
        guest_scp_from "$ip" \
            "proxytools-build/source/release/bin/windows/$file" "$ARTIFACTS/$file"
    done
    retrieve_logs "$LOGS/guest"
    (cd "$ARTIFACTS" && sha256sum -c SHA256SUMS)
}

on_exit() {
    local status=$?
    if (( status != 0 && RUN_STARTED )); then
        set +e
        if [[ -n "$ACTIVE_VMID" ]]; then
            retrieve_logs "$LOGS/failed-$ACTIVE_VMID"
            printf 'pve-windows-build: failed clone retained: VMID=%s name=%s IP=%s\n' \
                "$ACTIVE_VMID" "$ACTIVE_NAME" "${ACTIVE_IP:-unknown}" >&2
        fi
        printf 'pve-windows-build: diagnostics retained in %s\n' "$WORK" >&2
    fi
    release_pve_lock
}
trap on_exit EXIT

main() {
    local command
    while (( $# )); do
        case "$1" in
            --release) RELEASE_MODE=1; shift ;;
            -h|--help) printf 'Usage: %s [--release]\n' "$0"; return ;;
            *) fail "unknown argument: $1" ;;
        esac
    done
    for command in awk git scp sed sha256sum ssh ssh-keygen tar; do
        command -v "$command" >/dev/null || fail "required command not found: $command"
    done
    [[ -f "$PVE_ROOT_KEY" ]] || fail "PVE root key not found: $PVE_ROOT_KEY"
    [[ -f "$GUEST_KEY" ]] || fail "guest key not found: $GUEST_KEY"

    cd "$ROOT"
    SOURCE_COMMIT=$(git rev-parse HEAD)
    if [[ -z "$(git status --porcelain)" ]]; then SOURCE_TREE=clean; else SOURCE_TREE=dirty; fi
    acquire_pve_lock
    if (( RELEASE_MODE )) && [[ "$SOURCE_TREE" != clean ]]; then
        fail 'release mode requires a clean worktree, including no untracked files'
    fi
    RUN_STARTED=1
    validate_template
    cleanup_stale_clones
    rm -rf -- "$WORK" "$BIN"
    mkdir -p -- "$WORK" "$LOGS" "$BIN_ROOT"
    touch "$KNOWN_HOSTS"
    chmod 0600 "$KNOWN_HOSTS"
    prepare_source
    create_clone
    run_windows_build "$ACTIVE_IP"
    cleanup_active_clone
    mv -- "$ARTIFACTS" "$BIN"
    release_pve_lock
    trap - EXIT
    printf 'PVE Windows artifact: %s\n' "$BIN/proxytools.exe"
    printf 'PVE logs: %s\n' "$LOGS"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
