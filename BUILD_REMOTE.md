# Maintainer remote Linux build lab

This optional maintainer-only layer covers the project's isolated Linux build
environment on Proxmox VE. It is not required for contributors, local testing,
or pull requests. The templates and automated clone, transfer, build,
cross-distribution validation, collection, and cleanup workflow are operational.

The remote workflow will run the same `release/linux/build.sh` and
`release/linux/smoke.sh` documented in `BUILD_LOCAL.md`. It must not grow a
second guest-only implementation.

## Build server

The development machine reaches the PVE host with:

```bash
ssh root@192.168.66.2
```

Current host baseline:

- ThinkPad X230 with Intel Core i5-3230M and VT-x;
- Proxmox VE 9.2 on Debian 13;
- 4 logical CPUs and 8 GiB RAM;
- `local-lvm`, approximately 49 GiB LVM-thin storage for VM disks;
- `local`, approximately 37 GiB directory storage for images and snippets;
- guest DHCP through `vmbr0` on `192.168.66.0/24`.

Run Linux and future Windows builders sequentially on this host. Use PVE's
`qm` and `pvesm`; do not install a second libvirt stack and do not use LXC for
native release builds.

## Rebuild or migrate the PVE build lab

The versioned bootstrap script recreates the Linux template layer on a clean
PVE host:

```bash
scp release/pve/provision-host.sh root@PVE_HOST:/root/
scp ~/.ssh/proxytools-build.pub root@PVE_HOST:/root/
ssh root@PVE_HOST \
  /root/provision-host.sh \
    --ssh-public-key /root/proxytools-build.pub
```

Before running it, install PVE normally and configure these host resources:

- `local` directory storage with `iso`, `snippets`, and `import` content;
- `local-lvm` storage for VM disks;
- `vmbr0` with DHCP connectivity for guests.

Those installer- and site-specific storage/network decisions are deliberately
not rewritten by the project script. The bootstrap validates them, downloads
the current official Debian 13 and Ubuntu 24.04 release cloud images, checks
their published SHA-512/SHA-256 values, installs the required guest packages,
cleans per-instance cloud-init and SSH identity, and creates protected stopped
templates `9000` and `9001`. Each newly created template must also boot one
automatically selected disposable linked clone, pass guest-agent, cloud-init,
OS/tool checks, shut down, and clean up that exact clone.

Provisioning also enables LVM thin-pool automatic growth at 80% usage in 20%
increments and restarts its monitor. It replaces any active values with one
marked Proxy Tools block, validates the effective values after writing, and
keeps a timestamped copy of `/etc/lvm/lvm.conf` beside that file. The volume
group must retain free extents for automatic growth to work.

The script is safe to rerun: exact valid templates are checked and left alone.
It never replaces an occupied VMID or an existing image with a bad/currently
different checksum. A failed provisioning VM is retained intact for diagnosis.
Check an existing host without downloading or creating anything with:

```bash
ssh root@PVE_HOST /root/provision-host.sh --check-only
```

`--check-only` is strictly read-only. It validates that thin-pool autoextend is
already set to `80/20` along with storage, bridge, and template invariants; it
never repairs configuration, downloads images, creates VMs, or restarts a
service.

The public guest key may be copied to the PVE host temporarily; never copy its
private half. Remove the temporary public-key copy after successful bootstrap
if it is not otherwise useful. Official cloud images remain cached under
`/var/lib/vz/template/iso/` for recovery and deliberate template maintenance.

## Linux template

The protected template is:

| Property | Value |
|---|---|
| VMID | `9000` |
| Name | `proxytools-linux-template` |
| OS | Debian 13 stable amd64 generic cloud image |
| CPU | 2 vCPU, host type |
| RAM | 3 GiB, no ballooning |
| Disk | 20 GiB on `local-lvm` |
| Network | VirtIO on `vmbr0`, DHCP |
| Access | `builder` with dedicated SSH key |
| State | stopped, `template=1`, `protection=1` |
| Cloud-init upgrades | disabled (`ciupgrade=0`) |

The template contains Python, venv/dev headers, build-essential, Git, rsync,
OpenSSH, CA certificates, curl, file, and QEMU Guest Agent. It does not contain
the project source, a project virtual environment, Python project dependencies,
PyInstaller, or versioned build/test scripts.

The private guest key stays on the development machine:

```text
~/.ssh/proxytools-build
```

Ephemeral guest host keys use a separate file:

```text
~/.cache/proxytools-build/known_hosts
```

Never boot or build directly in template `9000`. Template maintenance is an
explicit task followed by clean shutdown and validation through a disposable
clone.

## Linux compatibility template

The second protected template checks that the standalone binary also runs on
the current Ubuntu LTS baseline:

| Property | Value |
|---|---|
| VMID | `9001` |
| Name | `proxytools-ubuntu-2404-check-template` |
| OS | Ubuntu 24.04 LTS amd64 cloud image |
| CPU | 2 vCPU, host type |
| RAM | 2 GiB, no ballooning |
| Disk | 20 GiB on `local-lvm` |
| Network | VirtIO on `vmbr0`, DHCP |
| Access | `builder` with the same dedicated SSH key |
| State | stopped, `template=1`, `protection=1` |

This is a runtime compatibility target, not a second build environment. It
contains QEMU Guest Agent and CA certificates but no project source or frozen
artifact. Create a disposable linked clone, transfer the complete artifact
set, verify `SHA256SUMS`, and exercise the binary there. The initial validation
successfully ran the Debian 13-built executable on Ubuntu 24.04 with glibc
2.39, including `--version`, `--about`, and help for both modes.

Never boot template `9001` directly. The current host may retain provisioning
vendor data under `local:snippets/`; the bootstrap script uses temporary vendor
data only while baking packages and detaches it before templating. Validate
changes through an unprotected disposable linked clone and delete only that
exact clone after a clean shutdown.

## Manual linked-clone lifecycle

The following documents the mechanism used by the automated workflow and is
useful for diagnosis or explicit template maintenance.

Choose an unused VMID and an exact disposable name. Verify the build template
first:

```bash
ssh root@192.168.66.2 qm status 9000
ssh root@192.168.66.2 qm config 9000 \
  | grep -E '^(name|template|protection):'
```

Require `status: stopped`, `template: 1`, `protection: 1`, and the exact name
`proxytools-linux-template`. Then create the linked clone:

```bash
ssh root@192.168.66.2 \
  qm clone 9000 VMID --name proxytools-linux-vX.Y.Z --full 0
ssh root@192.168.66.2 qm set VMID --protection 0
ssh root@192.168.66.2 qm start VMID
```

Do not pass `--storage` to a linked clone. PVE keeps its thin overlay with the
template base volume. Clones inherit template protection, so disable protection
only on the exact disposable VMID immediately after cloning.

Poll the QEMU Guest Agent with a bounded timeout and select the non-loopback
IPv4 address:

```bash
ssh root@192.168.66.2 qm agent VMID ping
ssh root@192.168.66.2 qm agent VMID network-get-interfaces
```

Do not scan the LAN. If the agent never appears, report `qm status VMID` and
inspect the PVE console.

PVE currently emits a deprecated scalar `user` in NoCloud data. Debian 13 may
therefore report `extended_status: degraded done` and status code 2 with
`errors: []`. Accept only that known deprecation; any actual or additional
cloud-init error fails provisioning.

## Source transfer and build

The normal maintainer command from the development checkout is:

```bash
./release/pve/build.sh
```

The build driver and mutating host provisioning share one kernel lock on the
PVE host. Only one build or provisioning operation can own the lab at a time; a second
invocation exits before deleting local output, logs, or VMs. The lock follows
the owning SSH process and is released automatically if that process exits.
The strictly read-only `--check-only` validation does not create or update the
lock file.

It accepts the current worktree, including uncommitted development changes.
At startup its local cleanup removes only the previous
`release/.work/pve-linux/` and `release/bin/`. It also finds PVE clones left by
earlier failed runs using the exact
owned names `proxytools-debian-build-VMID` and
`proxytools-ubuntu-validation-VMID`, validates their VMID, name, non-template
state, and disabled protection, then shuts them down and deletes them before
creating new work. It then:

1. validates protected templates `9000` and `9001`;
2. builds and tests in a disposable Debian linked clone;
3. runs Debian live smoke and retrieves the artifact and full logs;
4. deletes the successful Debian clone;
5. runs the same artifact through offline and live smoke in a disposable
   Ubuntu 24.04 linked clone;
6. deletes the successful Ubuntu clone;
7. promotes the verified artifact set to `release/bin/`.

Logs are retained under `release/.work/pve-linux/logs/{debian,ubuntu}/`. On a
failure, the current clone and available diagnostics are retained and its exact
VMID, name, and IP are printed. Previously successful stages may already have
been cleaned because their artifacts and logs are safely back on the
development machine. The retained failed clone is automatically reconciled at
the beginning of the next run; unrelated VMs, protected VMs, templates, and
owned-looking names that do not end in their exact VMID are never deleted.

Connection defaults may be changed without editing the script:

```bash
PROXYTOOLS_PVE_HOST=root@PVE_HOST \
PROXYTOOLS_PVE_ROOT_KEY=/path/to/pve-root-key \
PROXYTOOLS_PVE_GUEST_KEY=/path/to/proxytools-build-key \
  ./release/pve/build.sh
```

The default PVE host is `root@192.168.66.2`; default keys are
`~/.ssh/id_rsa` for the host and `~/.ssh/proxytools-build` for guests. The
script keeps ephemeral guest host keys in its ignored `.work` directory and
handles DHCP address reuse between successive clones.

Offline destructive-guard regressions are available with:

```bash
./release/pve/test_pve_build.sh
./release/pve/test_provision_host.sh
```

During infrastructure development, the current worktree can be copied to a
disposable clone for smoke testing:

```bash
rsync -a --delete \
  --exclude=.venv/ --exclude=.git/index.lock \
  --exclude=proxydb/ --exclude=geodb/ \
  --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude=release/.work/ \
  -e 'ssh -i ~/.ssh/proxytools-build \
    -o UserKnownHostsFile=~/.cache/proxytools-build/known_hosts' \
  ./ builder@VM_IP:/home/builder/proxytools/
```

Inside the clone, run:

```bash
cd /home/builder/proxytools
./release/linux/build.sh
```

A release candidate uses the explicit clean-snapshot mode:

```bash
./release/pve/build.sh --release
```

It refuses tracked or untracked worktree changes before local cleanup, creates
one `git archive` from `HEAD`, records its SHA-256 value, and verifies that
archive independently inside both guests before extraction. The embedded and
manifest source commit comes from that archive and its tree state is `clean`.
The normal no-option command intentionally remains the dirty-worktree-capable
development path. The orchestrator retrieves the `proxytools` artifact, user
`README.md`, MIT `LICENSE`, `MANIFEST.txt`, `SHA256SUMS`, and full logs, then
independently verifies returned checksums. Live smoke remains a separate
network-dependent gate from the deterministic build result.

## Safe cleanup

Successful clones are disposable. A failed clone may remain briefly for
diagnosis. Before deletion, resolve and inspect the exact VMID:

```bash
ssh root@192.168.66.2 qm list
ssh root@192.168.66.2 qm config VMID
```

Refuse cleanup when the VM is template `9000`, has `template=1`, remains
protected, or its name does not exactly match the expected disposable build
name. Shut the guest down and require `status: stopped` before deletion:

```bash
ssh -i "$HOME/.ssh/proxytools-build" builder@VM_IP sudo poweroff
ssh root@192.168.66.2 qm status VMID
ssh root@192.168.66.2 qm destroy VMID --purge 1
ssh-keygen \
  -f "$HOME/.cache/proxytools-build/known_hosts" -R VM_IP
```

Never use a glob, empty variable, template name, repository path, `/`, or
`$HOME` as a cleanup target.

## Recovery-drill evidence

The bootstrap was exercised from an empty Debian 13 VM converted to nested PVE
9.2.10 with KVM, isolated DHCP/NAT, `local`, and a dedicated `local-lvm` thin
pool. It created both templates, booted and removed both validation clones, and
then passed a second `--check-only` run without changing either template. The
outer drill VM and all of its virtual disks were deleted after success; it is
test evidence, not another persistent build host or template.

## Remaining remote release work

Publication and tagging still wait for the Windows native build stage. Both
platforms must consume the same clean snapshot and pass their native gates
before release publication. Windows remains a separate later stage.
