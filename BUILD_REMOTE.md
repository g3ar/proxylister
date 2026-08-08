# Maintainer remote Linux build lab

This optional maintainer-only layer covers the project's isolated Linux build
environment on Proxmox VE. It is not required for contributors, local testing,
or pull requests. The base template is operational; automated clone, transfer,
build, collection, and cleanup scripts are not implemented yet.

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

## Manual linked-clone lifecycle

The following documents the proven manual mechanism. It is not yet packaged as
release automation.

Choose an unused VMID and an exact disposable name. Verify the template first:

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

During infrastructure development, the current worktree can be copied to a
disposable clone for smoke testing:

```bash
rsync -a --delete \
  --exclude=.git/ --exclude=.venv/ \
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

A real release must not use a dirty-worktree rsync. Future orchestration must
create one clean, checksummed source archive from the release worktree, upload
it, verify it inside the clone, run the committed build script, retrieve the
artifact, user `README.md`, manifest, and full logs, then independently verify
returned checksums.

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

## Next remote stage

The next implementation is Linux-only orchestration:

```text
validate template and choose VMID
  -> create and unprotect linked clone
  -> wait for guest agent and SSH
  -> upload one clean source snapshot
  -> run the existing Linux build script
  -> retrieve and verify artifacts and logs
  -> delete a successful clone
```

It must use bounded waits, stop on the first failed gate, retain a useful failed
clone only for diagnosis, and never store private keys in Git. Windows remains
out of scope until this Linux flow is complete.
