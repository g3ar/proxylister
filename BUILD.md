# Proxy Tools release build lab

This document describes the release build and test environment used for the
standalone Proxy Tools executables. It is an operator runbook for a Debian 13
development host and a specification for the future automation under
`release/`.

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
Debian 13 build host
  ├── source repository and temporary release worktree
  ├── libvirt/KVM
  ├── immutable Linux builder template
  ├── immutable Windows builder template
  └── release/.work/                 temporary local staging
        ├── current/                 current build and logs
        └── failed/                  most recent failed build only
```

For every release, the operator creates one disposable clone of each template.
The exact same source archive is copied to both builders. Each VM runs its own
tests, produces its native executable, runs smoke tests without relying on the
developer's Python environment, and returns its artifact and logs to the host.

The disposable VMs are deleted after a successful release. On failure, retain
only the failed VM that is useful for immediate diagnosis; delete it when the
problem has been understood or before the next release attempt. VM images,
installation media, credentials, virtual environments, build output, and logs
must never be committed.

## 2. Host requirements

The examples assume:

- Debian 13 on an x86_64 machine;
- hardware virtualization enabled in UEFI/BIOS;
- a user with `sudo` access;
- enough resources for two builder VMs, although they may run sequentially;
- internet access from the VMs for Python dependencies and live smoke tests;
- the project cloned somewhere writable by the operator.

A practical minimum is 4 CPU cores, 16 GiB RAM, and 80 GiB free storage. A more
comfortable allocation is 8 or more cores, 32 GiB RAM, and SSD storage.

Verify virtualization before installing anything:

```bash
lscpu | grep -E 'Architecture|Virtualization'
test -e /dev/kvm && echo 'KVM device is available'
```

If `/dev/kvm` is absent, enable Intel VT-x or AMD-V in firmware and ensure that
the `kvm_intel` or `kvm_amd` kernel module is loaded. Do not continue with pure
QEMU emulation: it is unnecessarily slow for this job.

## 3. Install QEMU and libvirt on Debian 13

Install the host-side tools:

```bash
sudo apt update
sudo apt install --yes \
  qemu-system-x86 \
  qemu-utils \
  libvirt-daemon-system \
  libvirt-clients \
  virtinst \
  virt-manager \
  virt-viewer \
  genisoimage \
  guestfs-tools \
  openssh-client
```

Enable libvirt and add the current user to the relevant groups:

```bash
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt,kvm "$USER"
```

Log out and back in so the new group membership is applied. Then verify the
system connection:

```bash
virsh --connect qemu:///system list --all
virt-host-validate qemu
```

Use `qemu:///system` consistently. Mixing the per-user `qemu:///session`
instance with the system instance is a common source of missing networks,
images, and permissions.

The default libvirt NAT network is suitable here: builders can reach the
internet and the host can reach them through their private addresses, while no
service is exposed directly on the physical LAN.

```bash
virsh --connect qemu:///system net-start default 2>/dev/null || true
virsh --connect qemu:///system net-autostart default
virsh --connect qemu:///system net-list --all
```

## 4. Directory and naming conventions

Keep large VM data outside the Git repository. One possible host layout is:

```text
/var/lib/libvirt/images/
  proxytools-linux-template.qcow2
  proxytools-windows-template.qcow2
  proxytools-linux-vX.Y.Z.qcow2       disposable
  proxytools-windows-vX.Y.Z.qcow2     disposable

project checkout/
  BUILD.md
  release/                            future committed automation only
    README.md
    linux/
    windows/
    vm/
    pyinstaller/
    host/
    .work/                            ignored, temporary artifacts/logs
```

Recommended libvirt domain names are:

- `proxytools-linux-template`;
- `proxytools-windows-template`;
- `proxytools-linux-vX.Y.Z`;
- `proxytools-windows-vX.Y.Z`.

Never run builds in a template. Shut it down cleanly after provisioning and
treat it as immutable. Update a template through an explicit maintenance
session, shut it down again, then take a backup before the next release.

## 5. SSH identity for disposable builders

Use a dedicated key, not a personal SSH identity. This key is local build-lab
state and must not enter Git:

```bash
install -d -m 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/proxytools-build" \
  -C proxytools-build -N ''
```

The private key stays on the Debian host. Put only its public half in both VM
templates. Because the guests are ephemeral and use recycled addresses, keep a
separate known-hosts file rather than weakening host-key checks globally:

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

Use a conservative x86_64 Linux userspace for the release binary. A binary
built against an older glibc generally runs on newer distributions, while the
reverse is not guaranteed. The exact supported glibc baseline must be fixed
before the first standalone release; Debian 11 is a reasonable candidate for a
`glibc >= 2.31` baseline. Do not silently change this image between releases.

Create the VM with `virt-install` or virt-manager. A typical allocation is:

- 2 to 4 vCPUs;
- 4 GiB RAM;
- 24 GiB qcow2 disk;
- default NAT network;
- minimal server installation, no desktop required.

Example interactive creation, after downloading installation media outside the
repository:

```bash
sudo virt-install \
  --connect qemu:///system \
  --name proxytools-linux-template \
  --memory 4096 \
  --vcpus 4 \
  --cpu host-passthrough \
  --disk path=/var/lib/libvirt/images/proxytools-linux-template.qcow2,size=24,format=qcow2 \
  --cdrom /path/to/debian-installer.iso \
  --network network=default,model=virtio \
  --graphics spice \
  --os-variant debian11
```

Inside the guest, create a dedicated `builder` account and install only the
native build prerequisites:

```bash
sudo apt update
sudo apt install --yes \
  python3 \
  python3-venv \
  python3-dev \
  build-essential \
  openssh-server \
  ca-certificates
sudo systemctl enable --now ssh
```

Copy the dedicated public key into `/home/builder/.ssh/authorized_keys`, with
directory mode `0700` and file mode `0600`. Confirm a key-based login from the
host before sealing the template.

Chrome is not needed to compile Selenium into the executable. Install a browser
in the builder only if a release smoke test explicitly exercises browser
launching. Browser availability is otherwise an end-user runtime concern.

Record the baseline for later diagnostics:

```bash
cat /etc/os-release
ldd --version | head -n 1
python3 --version
```

Finally, clean package caches, shut the guest down, and back up its qcow2 image.

## 7. Windows builder template

Use a Windows 10 x86_64 installation image supplied and licensed by the
operator. A typical allocation is:

- 4 vCPUs;
- 8 GiB RAM;
- 50 GiB qcow2 disk;
- default NAT network;
- VirtIO storage and network drivers where available.

Create it with virt-manager or `virt-install`. Interactive installation through
virt-manager is usually easier because Windows may need the VirtIO driver ISO
during setup. The resulting libvirt domain must be named
`proxytools-windows-template`.

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

Verify from the Debian host that SSH, SCP, PowerShell, and Python all work:

```bash
ssh -i "$HOME/.ssh/proxytools-build" builder@WINDOWS_IP \
  'powershell -NoProfile -Command "$PSVersionTable.PSVersion; py -3 --version"'
```

Do not install a project `.venv` into the image. A fresh virtual environment is
created inside every disposable release VM and disappears with it. Shut down
the provisioned template cleanly and back up its qcow2 image.

## 8. Discovering VM addresses

Start a guest and ask the libvirt guest agent or DHCP lease table for its
address:

```bash
virsh --connect qemu:///system start VM_NAME
virsh --connect qemu:///system domifaddr VM_NAME --source lease
virsh --connect qemu:///system net-dhcp-leases default
```

Installing `qemu-guest-agent` in Linux and the corresponding Windows guest
agent improves address discovery and clean shutdown, but DHCP lease lookup is a
valid fallback. Automation should poll with a bounded timeout and print the VM
console location on failure; it must not wait forever.

Before reusing an address, remove only that address from the dedicated
known-hosts file:

```bash
ssh-keygen -f "$HOME/.cache/proxytools-build/known_hosts" -R VM_IP
```

## 9. Creating disposable release VMs

The simplest reliable first implementation is a full `virt-clone`. It consumes
more disk than a linked clone but does not risk coupling a running release VM
to accidental template changes.

```bash
sudo virt-clone \
  --connect qemu:///system \
  --original proxytools-linux-template \
  --name proxytools-linux-vX.Y.Z \
  --auto-clone

sudo virt-clone \
  --connect qemu:///system \
  --original proxytools-windows-template \
  --name proxytools-windows-vX.Y.Z \
  --auto-clone
```

Once the manual process is proven, the automation may use qcow2 overlays for
faster cloning. That optimization must preserve immutable backing images and
must validate the resolved backing path before deleting an overlay.

Never clone a running or suspended template. Both templates must be shut off:

```bash
virsh --connect qemu:///system domstate proxytools-linux-template
virsh --connect qemu:///system domstate proxytools-windows-template
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

Create one archive and one checksum on the host, then send those exact files to
both builders:

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

The archive excludes untracked local state by construction. Verify its checksum
inside each guest before extracting it.

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

Generate `SHA256SUMS` on the Debian host only after both artifacts arrive:

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

Resolve and inspect exact domain names before deleting anything:

```bash
virsh --connect qemu:///system list --all
virsh --connect qemu:///system domblklist proxytools-linux-vX.Y.Z
virsh --connect qemu:///system domblklist proxytools-windows-vX.Y.Z
```

Then shut down the disposable guests. Use `destroy` only when a guest will not
shut down and its loss is explicitly acceptable:

```bash
virsh --connect qemu:///system shutdown proxytools-linux-vX.Y.Z
virsh --connect qemu:///system shutdown proxytools-windows-vX.Y.Z
```

After confirming both are stopped, remove the disposable domains and their
managed storage:

```bash
virsh --connect qemu:///system undefine \
  proxytools-linux-vX.Y.Z --remove-all-storage
virsh --connect qemu:///system undefine \
  proxytools-windows-vX.Y.Z --remove-all-storage --nvram
```

Use `--nvram` only for a domain whose inspected definition contains an NVRAM
file. It is typical for a UEFI Windows guest but may not exist for a BIOS Linux
guest.

Never substitute a glob, empty variable, template name, `/`, `$HOME`, or the
repository root into a cleanup command. Future automation must require the
`proxytools-{linux,windows}-vX.Y.Z` naming pattern, refuse template domains, and
print resolved disks before deletion.

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
  vm/clone-linux.sh
  vm/clone-windows.sh
  vm/wait-for-ssh.sh
  vm/destroy.sh
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
