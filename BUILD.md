# ProxyLister single-executable builds

This is the only build runbook. It covers local and PVE-assisted creation and
testing of standalone executables. Normal users either clone the repository and
run `./proxylister`, or download a prepared binary as described in `README.md`;
they do not need this document.

## Current status

| Stage | Status |
|---|---|
| Cross-platform Python build/PVE control plane | Implemented |
| Local Linux x86_64 build and offline frozen smoke | Implemented |
| Optional local Linux live smoke | Implemented |
| Debian 13 PVE native build | Implemented |
| Ubuntu 24.04 LTS PVE compatibility smoke | Implemented |
| Clean checksummed PVE release snapshot | Implemented with `--release` |
| Windows ready-state template | Implemented and validated on VMID 9002 |
| Windows 11 PVE native build, offline smoke, and live smoke | Implemented |
| GitHub Release packaging and publication | Implemented |

Do not describe a planned stage as operational. Update this table when a stage
actually becomes usable.

## Shared release rules

A local dirty-worktree build is useful for development but is not publishable.
A release build starts from one clean release worktree and records the exact
commit. Linux and Windows must eventually consume the same checksummed source
snapshot and pass native automated tests.

Each native builder must:

1. create a fresh build environment;
2. run the complete source tests and validation;
3. build from the committed PyInstaller definition;
4. test the executable without the source tree or build environment in `PATH`;
5. return the executable, root user guide, `LICENSE`, manifest, checksums, and
   complete logs.

Platform outputs are independent:

```text
release/bin/linux/
release/bin/windows/
```

The Linux executable is named `proxylister`; the Windows executable is named
`proxylister.exe`. Do not add OS, architecture, or version suffixes to either
directory or executable name. Each platform directory carries its own
`README.md`, `LICENSE`, `MANIFEST.txt`, and `SHA256SUMS`; OS, architecture,
version, source identity, and build-tool identity belong in the executable
metadata and that platform's manifest. A platform build may replace only its
own output directory and must preserve the other platform's successful output.

Generated environments, VM images, credentials, artifacts, and logs are local
state and must not be committed. `pyproject.toml` remains the only project and
dependency manifest. Exact Linux build dependencies are locked in
`release/linux/constraints.txt`.

All build and build-lab orchestration starts through the cross-platform Python
entrypoint `release/build.py`. Shared implementation lives under
`release/buildlib/`; `release/smoke.py` owns native frozen smoke on both
platforms. Bash is not part of the build implementation. `Autounattend.xml` and
`release/pve/windows/bootstrap.ps1` remain narrow Windows Setup inputs because
they execute before Python is available in a new Windows guest.
The examples use `python3`; on Windows use the equivalent `py -3` command.

## Local Linux build

From anywhere inside the checkout, run:

```bash
python3 release/build.py build linux
```

Use this when changing packaging, startup, bundled resources, runtime paths,
configuration bootstrap, PyInstaller-sensitive imports, or before handing off
a release-related change. It accepts a dirty worktree so work in progress can
be tested.

The Python builder removes its previous `release/.work/local-linux/` and
`release/bin/linux/`, creates a fresh release-only virtual environment,
enforces the locked dependencies, runs source validation, builds the one-file
executable, and runs deterministic offline frozen smoke. Only a fully
successful artifact set is promoted to `release/bin/linux/`.

The result contains:

- `proxylister`;
- a generated copy of the root `README.md` for the distribution;
- `LICENSE`;
- `MANIFEST.txt`;
- `SHA256SUMS`.

`MANIFEST.txt` records the project version, source commit, clean/dirty state,
UTC build time, platform, Python, pip, PyInstaller, and constraints checksum.
The executable reports embedded build identity through `--about`. Detailed
output remains in `release/.work/local-linux/logs/build.log`.

Rerun deterministic frozen smoke against the current artifact without a new
build:

```bash
python3 release/smoke.py offline release/bin/linux/proxylister
```

Run the bounded network-dependent smoke separately:

```bash
python3 release/smoke.py live release/bin/linux/proxylister
```

The live list check fetches the real candidate set, starts checking it, waits
until at least two valid proxies have been found, requests the normal bounded
`Ctrl+C` shutdown, and requires stdout to match `working_proxies.txt` exactly.
It uses a relaxed 5000 ms acceptance ceiling so this cross-platform output gate
does not depend on the route latency of a particular build guest; the product's
configured default is unchanged.
The minimum can be overridden with `--minimum-proxies` when diagnosing a live
environment. Its list and monitor logs remain in
`release/.work/local-linux/logs/live-list.log` and `live-monitor.log`, including
after failure. Interactive browser and TUI acceptance remain manual.

## PVE Linux build

The maintainer host is configured by `PVE_HOST` in `release/build_config.py` and
is always accessed as `root`. Change that one value when the dedicated PVE
build-lab server moves. PVE is not required for ordinary development. The build
lab uses these stopped, protected, immutable templates:

| VMID | Name | Purpose |
|---|---|---|
| `9000` | `proxylister-linux-template` | Debian 13 native build and test |
| `9001` | `proxylister-ubuntu-2404-check-template` | Ubuntu 24.04 LTS compatibility smoke |

Template names, disposable clones, the shared host lock, artifacts, environment
variables, and documentation all use the `proxylister` project name.

Never boot, build in, modify, or delete either base template during an ordinary
build. Run the development workflow through disposable linked clones:

```bash
python3 release/build.py build linux --pve
```

The orchestrator transfers the current worktree, builds and tests it in a
Debian clone, retrieves the artifact and logs, then validates that exact
artifact through offline and live smoke in an Ubuntu clone. It promotes output
to `release/bin/linux/` only after both operating-system gates pass.

Successful exact clones are shut down and deleted. The active clone and
available diagnostics are retained on failure. At the beginning of the next
run, only exact unprotected non-template clones named
`proxylister-debian-build-VMID` or `proxylister-ubuntu-validation-VMID` may be
reconciled automatically. Unrelated VMs and protected templates are never
cleanup targets.

Logs remain under `release/.work/pve-linux/logs/{debian,ubuntu}/`. The PVE host
has one source of truth:

```python
# release/build_config.py
PVE_HOST = "PVE_HOST_OR_IP"
```

Override credential paths without editing the code when necessary:

```bash
PROXYLISTER_PVE_ROOT_KEY=/path/to/pve-root-key \
PROXYLISTER_PVE_GUEST_KEY=/path/to/proxylister-build-key \
PROXYLISTER_PVE_KNOWN_HOSTS=/path/to/known_hosts \
  python3 release/build.py build linux --pve
```

The default host key is `~/.ssh/id_rsa`; the guest key is
`~/.ssh/proxylister-build`. Ephemeral guest host keys stay in ignored build
work.

For a publishable candidate, use the explicit clean-snapshot mode:

```bash
python3 release/build.py build linux --pve --release
```

It refuses tracked or untracked worktree changes before cleanup, creates one
checksummed `git archive` from `HEAD`, and verifies that same archive in both
guests. Build and provisioning operations share a PVE-side kernel lock; a
contender exits before touching local output or VMs.

### Provision or audit the Linux templates

PVE storage and `vmbr0` networking are site-specific prerequisites and must
already be configured. The same Python entrypoint bundles the provisioner,
copies it and only the public half of the dedicated guest key to a temporary
PVE-host directory, runs it there, and removes that temporary directory:

```bash
python3 release/build.py provision linux \
  --ssh-public-key ~/.ssh/proxylister-build.pub
```

The bootstrap validates host prerequisites, verifies official cloud-image
checksums, creates and validates protected templates `9000` and `9001`, and
retains failed provisioning state for diagnosis. It never replaces an occupied
VMID or a mismatched cached image automatically.

Audit the existing host without downloading, creating, or changing anything:

```bash
python3 release/build.py provision linux --check-only
```

Run infrastructure checks explicitly; they are not part of the Python source
test suite:

```bash
python3 -m unittest discover -s release/tests -v
python3 release/tests/pve_audit.py linux
```

The first command is the offline shared build/PVE guard suite. The second
performs a read-only before/after comparison against the configured PVE host.
Release infrastructure tests live under `release/tests/`; the root `tests/`
directory remains only for source-application behavior.

### Cached installation media and safe cleanup

Verified cloud images, Windows installation ISOs, and versioned VirtIO driver
ISOs under `/var/lib/vz/template/iso/` are persistent build-lab state. Validate
their recorded upstream checksum and reuse them. Automation may remove
incomplete `.part` files and generated unattended-answer media, but must never
delete verified source media.

Before purging a disposable VM, detach every cached ISO or cloud image and
verify that `qm config VMID` no longer references it. `qm destroy --purge 1`
can delete attached ISO volumes. Also refuse cleanup when the target is a base
template, has `template=1`, remains protected, or its exact name and VMID do not
match the expected disposable guest. Both the Linux build orchestrator and
template provisioner enforce this detach-and-recheck gate immediately before
their destructive cleanup.

## PVE Windows build

The repeatable template workflow under `release/pve/windows/` has produced the
stopped, protected template VMID `9002`. Run the development build
from anywhere in the checkout with:

```bash
python3 release/build.py build windows --pve
```

The orchestrator validates the immutable template, reconciles only exact stale
unprotected clones named `proxylister-windows-build-VMID`, and creates a fresh
linked clone. It verifies the template marker, QEMU Guest Agent, SSH, and
Python, then performs the normal online activation required by Enterprise
Evaluation and rejects a guest that is not licensed or has no remaining grace
period. No product key is required.

The current worktree is transferred as one checksummed archive. Inside Windows,
the pipeline creates a clean build environment, enforces
`release/pve/windows/constraints.txt`, runs the native source tests, builds the
committed PyInstaller definition, and runs deterministic offline frozen smoke.
It then runs a bounded network-dependent live smoke against that same frozen
executable. Only after every gate passes are the artifact, documentation,
manifest, checksums, and logs returned and `release/bin/windows/` promoted.

The successful clone is shut down and deleted. A failed active clone and its
diagnostics are retained for investigation. Logs remain under
`release/.work/pve-windows/logs/`; the finished distribution is under
`release/bin/windows/`.

For a publishable candidate, use the clean-snapshot mode:

```bash
python3 release/build.py build windows --pve --release
```

It rejects tracked or untracked worktree changes and builds a checksummed
`git archive` from `HEAD`. To guarantee both platforms consume one physical
snapshot under one PVE lock, use:

```bash
python3 release/build.py build all --pve --release
```

Run Windows orchestration checks explicitly when changing its lifecycle,
transfer, build, or cleanup behavior:

```bash
python3 -m unittest discover -s release/tests -v
python3 release/tests/pve_audit.py windows
```

These are infrastructure checks and are intentionally separate from the root
Python source tests.

The target build environment is current Windows 11 Enterprise Evaluation;
LTSC 2024 evaluation media has an expired fixed build and must not be used.
Python 3.13 is pinned and Windows 11-only APIs must be avoided. The resulting
x86_64 executable is expected to work on Windows 10, but compatibility must not
be described as validated until the frozen artifact passes a native Windows 10
smoke run.

The Windows adapter reuses the shared Python lifecycle's safety contract:
immutable template, disposable clones, retained verified source media, bounded
waits, exact cleanup guards, complete returned logs, and the same clean source
commit used for Linux. Windows-only unattended Setup assets remain under
`release/pve/windows/`; the lifecycle itself has one shared implementation.

Run the shared provision command with the dedicated public key:

```bash
python3 release/build.py provision windows \
  --ssh-public-key ~/.ssh/proxylister-build.pub
```

The provisioner verifies or downloads pinned media, creates only the exact
unprotected candidate VMID `9002`, completes unattended Setup and bootstrap,
observes the ready marker before accepting a clean shutdown, validates a fresh
linked clone, deletes that successful clone, and only then protects the base
template. A failure retains the exact active candidate for diagnosis.

Audit the resulting template and cached media without changing the host:

```bash
python3 release/build.py provision windows --check-only
python3 release/tests/pve_audit.py windows
```

The template is built from pinned official sources: the current Windows 11
Enterprise Evaluation x64 ISO and its published Microsoft SHA256, the
versioned stable upstream virtio-win ISO, the Microsoft OpenSSH Windows
implementation as a version-pinned MSI from the official
PowerShell/Win32-OpenSSH releases with its published GitHub digest, and a pinned
Python 3.13 x64 installer from python.org with its published SHA256. Do not use
the Windows Update-backed OpenSSH optional capability. Do not use third-party
Windows images, mirrors, package bootstrap services, or floating `latest`
artifacts. A Microsoft download that cannot be fetched non-interactively may
be uploaded manually only when it is the exact official artifact and passes
the pinned official checksum.

Do not run cumulative Windows Update while preparing this short-lived build
template. Install only the required components and pinned tools, prevent
automatic OS updates and update-triggered reboots in build clones, and refresh
the template deliberately from newer verified Microsoft evaluation media when
needed. Cache and retain the verified Windows, VirtIO, Win32-OpenSSH, and Python
source media on the PVE host. Generated unattended-answer media is temporary.

Keep `Autounattend.xml` limited to deterministic Windows Setup, a local build
administrator, and one bootstrap invocation. A single audited PowerShell
bootstrap installs the guest/build prerequisites, records the ready-state
contract, and shuts down. Each clone performs the normal online Evaluation
activation gate after boot; keeping it out of first-login setup avoids a
transient Windows licensing-service race.
Do not run Sysprep: the template is a prepared build appliance, and its linked
clones intentionally inherit the hostname, SID, and SSH host key. The PVE lock
permits only one Windows build clone at a time, so per-clone identity provides
no benefit and would add specialize/OOBE to every build startup. Because the
resulting Windows guests are isolated and disposable, the Windows template may
suppress SmartScreen, PowerShell execution-policy prompts, Defender real-time
build-path scanning, sleep, and automatic servicing that can interrupt a
build. This exception is Windows-only and changes no Linux host, template,
guest, or build security behavior. Do not weaken the provenance checks, PVE
firewall boundary, SSH key-only access, UEFI, TPM, or clone/template lifecycle
guards.

Release publication and tagging remain deferred until both platform binaries
pass their native gates. Manual TUI acceptance happens before the user declares
a release ready and is not part of release automation.

## Publish a GitHub Release

Publication is a separate retryable step after the clean all-platform build so
an upload failure never requires rebuilding the native artifacts:

```bash
python3 release/build.py build all --pve --release
python3 release/build.py publish
```

`publish` requires an authenticated official GitHub CLI (`gh auth login`), a
clean worktree, and a `vVERSION` tag. It verifies both platform checksum sets
and requires both manifests to identify that tag's clean commit and version.
This permits retrying publication after a later tooling-only commit without
rebuilding or retagging the binaries. It then creates these files under
`release/.work/publish/` and uploads them to a new GitHub Release:

- `proxylister-VERSION-linux-x86_64.tar.gz`;
- `proxylister-VERSION-windows-x86_64.zip`;
- `SHA256SUMS` covering both archives.

Each platform archive contains its executable, `README.md`, `LICENSE`,
`MANIFEST.txt`, and its platform-level `SHA256SUMS`. Publication fails closed
when the GitHub Release already exists; it never silently replaces published
assets.
