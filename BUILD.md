# Proxy Tools release build lab

This document describes the release build and test environment used for the
standalone Proxy Tools executables. It is an operator runbook for the Proxmox
VE build server at `192.168.66.2` and a specification for the future automation
under `release/`.

The supported standalone release targets are deliberately narrow:

- Linux x86_64;
- Windows 10 x86_64.

Both targets are built and tested in disposable QEMU/KVM virtual machines. We
do not mix containers and virtual machines in one release pipeline. macOS,
ARM, FreeBSD, and other platforms remain source-only targets: users may clone
the repository and build them themselves.

The ordinary development workflow does **not** use this lab. Continue using a
normal clone, `./proxytools`, and the local test suite between releases. Frozen
executables are built only after a version has been declared release-ready.

## 1. Release model

The lab consists of:

```text
development machine
  ├── source repository and temporary release worktree
  ├── dedicated build SSH identity
  ├── release/.work/                 temporary local staging
        ├── current/                 current build and logs
        └── failed/                  most recent failed build only

PVE build server (root@192.168.66.2)
  ├── immutable Linux builder template (VMID 9000)
  ├── immutable Windows builder template (planned)
  └── disposable LVM-thin linked clones
```

For every release, the operator creates one disposable clone of each template.
The exact same source archive is copied to both builders. Each VM runs its own
tests, produces its native executable, runs smoke tests without relying on the
developer's Python environment, and returns its artifact and logs to the host.

The disposable clones are deleted after a successful release. On failure, retain
only the failed VM that is useful for immediate diagnosis; delete it when the
problem has been understood or before the next release attempt. VM images,
installation media, credentials, virtual environments, build output, and logs
must never be committed.

Linux executables target current stable or LTS distributions. They are not
required to run on older distributions with older glibc; users of those systems
can run the source checkout with a compatible Python environment.

## 2. Build-server baseline

The current host is a ThinkPad X230 running Proxmox VE 9.2 on Debian 13:

- Intel Core i5-3230M, 4 logical CPUs, VT-x;
- 8 GiB RAM;
- `local-lvm`, approximately 49 GiB LVM-thin storage for VM disks;
- `local`, approximately 37 GiB directory storage for images and snippets;
- `vmbr0` on `192.168.66.0/24`, with DHCP available to guests.

Builders run sequentially on this host. Do not assume that Linux and Windows
builders can fit comfortably at the same time. Verify the host before a release:

```bash
pvesm status
qm list
free -h
```

If `/dev/kvm` is absent, enable Intel VT-x or AMD-V in firmware and ensure that
the `kvm_intel` or `kvm_amd` kernel module is loaded. Do not continue with pure
QEMU emulation: it is unnecessarily slow for this job.

## 3. PVE access and storage

The development machine reaches PVE with key-based SSH:

```bash
ssh root@192.168.66.2 pveversion
```

VM disks live on `local-lvm`. It is LVM-thin, so PVE creates fast linked clones
whose data volumes reference the immutable template base volume. ISO/cloud
images and cloud-init snippets live on `local`; its configured content types
include `iso` and `snippets`.

Do not install a second libvirt stack on PVE and do not manage these VMs with
`virsh`. Use PVE's `qm` and `pvesm` commands. Do not create LXC builders: native
Linux and Windows release builds use the same VM isolation model.

## 4. Directory and naming conventions

Keep all VM data outside the Git repository. The current layout is:

```text
/var/lib/vz/template/iso/
  debian-13-genericcloud-amd64.qcow2
/var/lib/vz/snippets/
  proxytools-linux-vendor.yaml         provisioning record
local-lvm:
  base-9000-disk-0                     immutable Linux template disk
  vm-<VMID>-disk-0                     disposable linked-clone overlay

project checkout/
  BUILD.md
  release/                            future committed automation only
    README.md
    linux/
    windows/
    pve/
    pyinstaller/
    host/
    .work/                            ignored, temporary artifacts/logs
```

PVE identities are:

- `9000`, `proxytools-linux-template`;
- a future reserved VMID and `proxytools-windows-template`;
- dynamically selected VMIDs named `proxytools-linux-vX.Y.Z` or
  `proxytools-windows-vX.Y.Z`.

Never run builds in a template. Template `9000` must remain stopped with
`template=1` and `protection=1`. Update it only through an explicit maintenance
procedure, validate a disposable clone afterward, and shut it down again.

## 5. SSH identity for disposable builders

Use a dedicated key, not a personal SSH identity. This key is local build-lab
state and must not enter Git:

```bash
install -d -m 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/proxytools-build" \
  -C proxytools-build -N ''
```

The private key stays on the development machine at
`~/.ssh/proxytools-build`. Put only its public half in VM templates. Because
guests are ephemeral and use recycled addresses, keep a separate known-hosts
file rather than weakening host-key checks globally:

```bash
install -d -m 700 "$HOME/.cache/proxytools-build"
touch "$HOME/.cache/proxytools-build/known_hosts"
chmod 600 "$HOME/.cache/proxytools-build/known_hosts"
```

Example SSH options used throughout this guide:

```text
-i ~/.ssh/proxytools-build
-o UserKnownHostsFile=~/.cache/proxytools-build/known_hosts
-o StrictHostKeyChecking=accept-new
```

Passwords are acceptable for the initial isolated-template setup, but the
release path should be key-based and non-interactive.

## 6. Linux builder template

The provisioned Linux template is VMID `9000`, based on the current official
Debian 13 stable generic cloud image. It has 2 vCPU, 3 GiB fixed RAM, a 20 GiB
thin disk, VirtIO networking on `vmbr0`, QEMU Guest Agent, DHCP, serial console,
and the non-root `builder` account.

The image contains only native build prerequisites:

```bash
sudo apt update
sudo apt install --yes \
  python3 \
  python3-venv \
  python3-dev \
  build-essential \
  git \
  rsync \
  openssh-server \
  ca-certificates \
  curl \
  file \
  qemu-guest-agent
sudo systemctl enable --now ssh
```

Python project dependencies, PyInstaller, the source tree, and versioned
build/test scripts are not installed in the template. Every clone receives the
current release snapshot and creates a fresh `.venv`. Cloud-init automatic
package upgrades are disabled (`ciupgrade=0`) so clone contents do not drift
during a release attempt.

Chrome is not needed to compile Selenium into the executable. Install a browser
in the builder only if a release smoke test explicitly exercises browser
launching. Browser availability is otherwise an end-user runtime concern.

Record the baseline for later diagnostics:

```bash
cat /etc/os-release
ldd --version | head -n 1
python3 --version
```

On 2026-08-08 the template was verified by creating linked clone `9001`,
transferring the current worktree with `rsync`, installing the project, and
passing the complete 77-test suite plus module compilation and launcher syntax
validation. The smoke clone was then deleted. Repeat an equivalent disposable-
clone test after every template maintenance session; do not treat the recorded
test count as a permanent expectation.

## 7. Windows builder template

The Windows template has not been provisioned yet. Use a Windows 10 x86_64
installation image supplied and licensed by the operator. Create it as a PVE
VM on `local-lvm`; do not introduce a separate hypervisor workflow. A typical
allocation is:

- 4 vCPUs;
- up to 6 GiB RAM on the current 8 GiB PVE host;
- 50 GiB thin disk;
- VirtIO networking on `vmbr0`;
- VirtIO storage and network drivers where available.

Use the PVE console for interactive installation because Windows may need the
VirtIO driver ISO during setup. Record the selected VMID here and in
`AGENTS.md` after provisioning, then protect and convert the stopped VM to a
PVE template.

Provision the template with:

- a local `builder` account;
- 64-bit Python in `PATH` for that account;
- Windows OpenSSH Server;
- current CA certificates and Windows updates appropriate to the chosen
  baseline;
- the dedicated build public key.

From an elevated PowerShell prompt, install and enable OpenSSH Server:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
if (-not (Get-NetFirewallRule -Name OpenSSH-Server-In-TCP -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name OpenSSH-Server-In-TCP `
        -DisplayName 'OpenSSH Server (sshd)' -Enabled True `
        -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
}
```

For a normal non-administrator account, place the public key in:

```text
C:\Users\builder\.ssh\authorized_keys
```

Restrict its ACL from PowerShell:

```powershell
icacls "$env:USERPROFILE\.ssh\authorized_keys" /inheritance:r
icacls "$env:USERPROFILE\.ssh\authorized_keys" /grant "${env:USERNAME}:(R)"
```

If `builder` is an administrator, Windows OpenSSH may instead use
`C:\ProgramData\ssh\administrators_authorized_keys`; consult the effective
`sshd_config`. A non-administrator build account avoids that special case.

Verify from the development machine that SSH, SCP, PowerShell, and Python all
work:

```bash
ssh -i "$HOME/.ssh/proxytools-build" builder@WINDOWS_IP \
  'powershell -NoProfile -Command "$PSVersionTable.PSVersion; py -3 --version"'
```

Do not install a project `.venv` or versioned build scripts into the image. A
fresh virtual environment and the current scripts arrive with the source
snapshot in every disposable clone. Shut the provisioned template down cleanly
and validate it through a linked clone before use.

## 8. Discovering VM addresses

Start a clone and ask its QEMU Guest Agent for its address:

```bash
ssh root@192.168.66.2 qm start VMID
ssh root@192.168.66.2 qm agent VMID ping
ssh root@192.168.66.2 qm agent VMID network-get-interfaces
```

Poll `qm agent VMID ping` with a bounded timeout, then select the non-loopback
IPv4 address. Do not scan the LAN. On failure, report `qm status VMID` and tell
the operator to inspect the PVE console; never wait forever.

PVE currently generates a deprecated scalar `user` field in its NoCloud data.
Debian 13 cloud-init therefore reports `extended_status: degraded done` and may
return status code 2 even though `errors: []`. Automation must inspect
`cloud-init status --long`: accept only this known deprecation with an empty
`errors` list, and fail for any real or additional provisioning error.

Before reusing an address, remove only that address from the dedicated
known-hosts file:

```bash
ssh-keygen -f "$HOME/.cache/proxytools-build/known_hosts" -R VM_IP
```

## 9. Creating disposable release VMs

Select an unused VMID and verify both its configuration path and intended name
before creation. Linux clones are linked LVM-thin clones of protected template
`9000`:

```bash
ssh root@192.168.66.2 \
  qm clone 9000 VMID --name proxytools-linux-vX.Y.Z --full 0
ssh root@192.168.66.2 qm set VMID --protection 0
ssh root@192.168.66.2 qm start VMID
```

Do not pass `--storage` for a linked clone: PVE requires its overlay to remain
on the template's thin storage. Template protection is inherited by clones, so
immediately disable protection on the exact disposable VMID; otherwise cleanup
will be blocked. Never disable protection on VMID `9000` during a build.

Before cloning, require the template to be stopped and protected:

```bash
ssh root@192.168.66.2 qm status 9000
ssh root@192.168.66.2 qm config 9000 \
  | grep -E '^(name|template|protection):'
```

## 10. Preparing a release source archive

Release work must not happen directly in the ordinary `main` checkout. Create
a separate worktree and temporary release branch:

```bash
git fetch origin
git worktree add -b release/vX.Y.Z \
  ../proxylister-release-vX.Y.Z origin/main
cd ../proxylister-release-vX.Y.Z
```

Make version and packaging fixes as small, ordinary commits on this branch.
Push the release branch for backup. Do not rebase, amend, force-push, or use
revert commits as a substitute for fixing and retesting the branch.

Before archiving, require a clean tree and record the exact commit:

```bash
test -z "$(git status --porcelain)"
git rev-parse HEAD
git diff --check
```

Create one archive and one checksum on the development machine, then send those
exact files to both builders:

```bash
mkdir -p release/.work/current/source
git archive --format=tar.gz \
  --prefix=proxytools-vX.Y.Z/ \
  --output=release/.work/current/source/proxytools-vX.Y.Z.tar.gz \
  HEAD
(
  cd release/.work/current/source
  sha256sum proxytools-vX.Y.Z.tar.gz > SOURCE.SHA256
)
```

The archive includes all committed release build/test scripts under `release/`
and excludes untracked local state by construction. Verify its checksum inside
each guest before extracting it. During infrastructure development, before the
release scripts exist, `rsync` from the current worktree is acceptable for a
disposable smoke clone:

```bash
rsync -a --delete \
  --exclude=.git/ --exclude=.venv/ \
  --exclude=proxydb/ --exclude=geodb/ \
  --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude=release/.work/ \
  -e 'ssh -i ~/.ssh/proxytools-build \
    -o UserKnownHostsFile=~/.cache/proxytools-build/known_hosts' \
  ./ builder@LINUX_IP:/home/builder/proxytools/
```

Do not use a dirty-worktree `rsync` transfer for a real release. A release uses
the single clean archive so Linux and Windows receive byte-identical source.

The repository currently has no frozen dependency lock or PyInstaller spec.
Those must be introduced under `release/` before the first real standalone
release. `pyproject.toml` remains the authoritative project manifest; a
release-only constraints/lock file records the exact resolved build inputs and
must not become a competing hand-maintained runtime manifest.

There are also two known application prerequisites for a Windows standalone
release. The current process lock uses Linux `flock` and must be replaced with
the already selected cross-platform `portalocker` implementation. The frozen
application must also carry embedded configuration defaults and create an
external `proxytools.conf` beside the executable on first use. Do not interpret
successful VM provisioning as proof that the current source tree is already
ready to freeze for Windows.

## 11. Linux build and tests

Copy the source bundle to the disposable Linux guest:

```bash
scp -i "$HOME/.ssh/proxytools-build" \
  -o UserKnownHostsFile="$HOME/.cache/proxytools-build/known_hosts" \
  release/.work/current/source/proxytools-vX.Y.Z.tar.gz \
  release/.work/current/source/SOURCE.SHA256 \
  builder@LINUX_IP:/tmp/
```

Inside the guest, the future `release/linux/build.sh` should perform these
steps in a fresh working directory:

1. verify `SOURCE.SHA256`;
2. extract the archive;
3. create a local `.venv`;
4. install the locked build dependencies and project;
5. run `python -m unittest discover -v`;
6. compile every Python module and validate the POSIX launcher;
7. build the one-file executable with the committed PyInstaller spec;
8. copy the executable to an output directory;
9. record Python, pip, PyInstaller, OS, glibc, source commit, and checksums.

Until those scripts exist, the equivalent project checks are:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .
./.venv/bin/python -m unittest discover -v
find src/proxytools -name '*.py' -print0 \
  | xargs -0 ./.venv/bin/python -m py_compile
sh -n proxytools
```

These commands validate the source project but do not yet produce the release
binary. Do not invent an ad-hoc PyInstaller command for an actual release: the
committed spec and hooks are part of the reviewed release definition.

## 12. Windows build and tests

Copy the same archive and checksum with `scp` to the Windows builder. OpenSSH
accepts paths such as `C:/Users/builder/build/`:

```bash
scp -i "$HOME/.ssh/proxytools-build" \
  -o UserKnownHostsFile="$HOME/.cache/proxytools-build/known_hosts" \
  release/.work/current/source/proxytools-vX.Y.Z.tar.gz \
  release/.work/current/source/SOURCE.SHA256 \
  builder@WINDOWS_IP:C:/Users/builder/build/
```

The future `release/windows/build.ps1` must mirror the Linux build semantics:

1. verify the SHA-256 checksum;
2. extract into a new directory;
3. create a fresh Windows `.venv`;
4. install the same locked project dependency versions plus the pinned native
   build tools;
5. run the complete unit test suite;
6. compile modules;
7. build `proxytools-vX.Y.Z-windows-x86_64.exe` from the committed spec;
8. record Windows, Python, pip, PyInstaller, source commit, and checksums.

Representative source-test commands in PowerShell are:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q src\proxytools
```

The POSIX root launcher is not tested on Windows; the frozen `.exe` is the
Windows entrypoint.

## 13. Frozen executable smoke tests

Passing source tests is not enough. Smoke tests must execute the frozen file
directly from a new writable directory where Python, the source tree, and the
build `.venv` are not on `PATH`.

Run at least:

- `proxytools[.exe] --version`;
- root, `list`, and `monitor` help;
- creation of the external default `proxytools.conf` on first normal run;
- creation of `proxydb/` and `geodb/` beside the executable;
- refusal to start a second process in the same directory;
- a bounded `list` network smoke test;
- startup, mount, key-binding registration, and clean shutdown of `monitor` in
  a PTY/console;
- `--clear` removing only documented local state;
- a clear failure when the executable directory is not writable.

Normal-mode runtime layout must be portable and directory-local:

```text
proxytools[.exe]
README.md
proxytools.conf
proxydb/
geodb/
```

The standalone executable contains Selenium unconditionally, but Chrome or
Firefox is external. Automated browser smoke tests may use a browser installed
in the guest to verify disposable profile creation and process startup. They
must not modify the guest's ordinary browser profile.

External services can fail independently of the binary. Separate deterministic
startup/configuration/locking tests from live ProxyScrape, GeoIP, identity, and
URL checks in the logs so an upstream outage is diagnosable rather than
misreported as a packaging failure.

Manual TUI acceptance is **not** part of this release pipeline. The maintainer
does that during development before declaring the version ready. Release smoke
tests cover only objective properties such as startup, mounting, bindings, and
clean termination.

## 14. Collecting artifacts

Copy successful outputs back to the release worktree:

```text
release/.work/current/artifacts/
  proxytools-vX.Y.Z-linux-x86_64
  proxytools-vX.Y.Z-windows-x86_64.exe
  README.md
  SHA256SUMS
```

Also collect machine-readable manifests and full logs while the release is in
progress:

```text
release/.work/current/logs/
  orchestrator.log
  linux-build.log
  linux-smoke.log
  windows-build.log
  windows-smoke.log
  linux-manifest.txt
  windows-manifest.txt
```

Generate `SHA256SUMS` on the development machine only after both artifacts
arrive:

```bash
cd release/.work/current/artifacts
sha256sum \
  proxytools-vX.Y.Z-linux-x86_64 \
  proxytools-vX.Y.Z-windows-x86_64.exe \
  README.md > SHA256SUMS
```

Check that each guest manifest names the same source commit and expected
version. A Linux success plus a Windows failure is a failed release, not a
partial release.

## 15. Failure handling

Logs exist to diagnose the most recent problem, not to become permanent local
state.

At the beginning of a new attempt:

1. delete the previous `release/.work/failed/` staging directory;
2. create a fresh `release/.work/current/`;
3. create new disposable VM clones.

If the attempt fails:

1. stop launching subsequent stages;
2. move `current/` to `failed/`;
3. retain relevant logs, manifests, source checksum, and any artifact already
   produced;
4. optionally retain the failing VM briefly for interactive diagnosis;
5. commit a normal fix on the release branch and start a complete new attempt.

Do not reuse a half-built virtual environment as proof that a clean release now
works. Do not publish only one platform.

## 16. Successful release sequence

The complete release gate is:

```text
clean release branch/worktree
  -> source tests
  -> Linux native build and frozen smoke tests
  -> Windows native build and frozen smoke tests
  -> collect both executables and README.md
  -> verify manifests and generate SHA256SUMS
  -> integrate the tested release branch into main
  -> create annotated version tag
  -> push main and tag
  -> create GitHub Release and upload four assets
  -> verify remote asset names and sizes
  -> delete local staging, successful logs, and disposable VMs
```

Prefer a fast-forward integration when `main` has not moved:

```bash
git switch main
git merge --ff-only release/vX.Y.Z
git push origin main
```

If `main` moved, merge it into the release branch with a normal merge commit and
rerun the entire build/test process. Do not rebase the already tested history.

Create the tag only after both native builds and their automated smoke tests
have passed and the tested commit is on `main`:

```bash
git tag -a vX.Y.Z -m 'Proxy Tools vX.Y.Z'
git push origin vX.Y.Z
```

Publish these GitHub Release assets:

- `proxytools-vX.Y.Z-linux-x86_64`;
- `proxytools-vX.Y.Z-windows-x86_64.exe`;
- `README.md`;
- `SHA256SUMS`.

After upload, query the release through the GitHub CLI or API and verify the
asset names and byte sizes before deleting local copies. Local artifacts and
successful logs are not archives; GitHub Release is their durable destination.

## 17. Safe VM destruction

Resolve and inspect the exact PVE VMID and name before deleting anything:

```bash
ssh root@192.168.66.2 qm list
ssh root@192.168.66.2 qm config VMID
```

Refuse cleanup if the resolved VM is a template, VMID `9000`, protected, or its
name does not exactly match the expected disposable build name. Ask the guest
to power off, then poll `qm status VMID` with a bounded timeout:

```bash
ssh -i "$HOME/.ssh/proxytools-build" builder@VM_IP sudo poweroff
ssh root@192.168.66.2 qm status VMID
```

After confirming `status: stopped`, remove that exact clone and its volumes:

```bash
ssh root@192.168.66.2 qm destroy VMID --purge 1
ssh-keygen \
  -f "$HOME/.cache/proxytools-build/known_hosts" -R VM_IP
```

Never substitute a glob, empty variable, template name, `/`, `$HOME`, or the
repository root into a cleanup command. Future automation must require the
`proxytools-{linux,windows}-vX.Y.Z` naming pattern, refuse template domains, and
print the resolved PVE configuration and disks before deletion.

## 18. What future automation must provide

When this manual process has worked end to end, scripts may be added under a
single `release/` directory. They should automate the runbook without hiding
the important gates:

```text
release/
  README.md                       script-specific usage
  orchestrate.sh                  host entrypoint
  linux/build.sh
  linux/smoke.sh
  windows/build.ps1
  windows/smoke.ps1
  pve/clone-linux.sh
  pve/clone-windows.sh
  pve/guest-ip.sh
  pve/destroy.sh
  vm/wait-for-ssh.sh
  pyinstaller/proxytools.spec
  pyinstaller/hooks/
  host/collect-artifacts.sh
  host/checksums.sh
  host/publish.sh
  .work/                          ignored
```

The orchestrator must be restart-safe, log every stage, use bounded waits,
verify source and artifact checksums, and stop on the first failed gate. It must
never store passwords or private keys in the repository. Destructive cleanup
must validate exact domain and disk targets before acting.

Automation may reduce typing; it must not weaken the core invariant: both
published executables were built from the same clean commit and passed their
tests on their native operating systems.
