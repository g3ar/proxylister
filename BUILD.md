# Proxy Tools single-executable builds

This is the only build runbook. It covers local and PVE-assisted creation and
testing of standalone executables. Normal users either clone the repository and
run `./proxytools`, or download a prepared binary as described in `README.md`;
they do not need this document.

## Current status

| Stage | Status |
|---|---|
| Local Linux x86_64 build and offline frozen smoke | Implemented |
| Optional local Linux live smoke | Implemented |
| Debian 13 PVE native build | Implemented |
| Ubuntu 24.04 LTS PVE compatibility smoke | Implemented |
| Clean checksummed PVE release snapshot | Implemented with `--release` |
| Windows template | Not implemented |
| Windows native build and smoke | Blocked on the accepted template |
| Release publication | Not implemented |

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

Generated environments, VM images, credentials, artifacts, and logs are local
state and must not be committed. `pyproject.toml` remains the only project and
dependency manifest. Exact Linux build dependencies are locked in
`release/linux/constraints.txt`.

## Local Linux build

From anywhere inside the checkout, run:

```bash
./release/linux/build.sh
```

Use this when changing packaging, startup, bundled resources, runtime paths,
configuration bootstrap, PyInstaller-sensitive imports, or before handing off
a release-related change. It accepts a dirty worktree so work in progress can
be tested.

The script removes its previous `release/.work/local-linux/` and
`release/bin/`, creates a fresh release-only virtual environment, enforces the
locked dependencies, runs source validation, builds the one-file executable,
and runs deterministic offline frozen smoke. Only a fully successful artifact
set is promoted to `release/bin/`.

The result contains:

- `proxytools`;
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
./release/linux/smoke.sh release/bin/proxytools
```

Run the bounded network-dependent smoke separately:

```bash
./release/linux/smoke-live.sh release/bin/proxytools
```

Its list and monitor logs remain in
`release/.work/local-linux/logs/live-list.log` and `live-monitor.log`, including
after failure. Interactive browser and TUI acceptance remain manual.

## PVE Linux build

The maintainer host is `root@192.168.66.2`. PVE is not required for ordinary
development. The build lab uses these stopped, protected, immutable templates:

| VMID | Name | Purpose |
|---|---|---|
| `9000` | `proxytools-linux-template` | Debian 13 native build and test |
| `9001` | `proxytools-ubuntu-2404-check-template` | Ubuntu 24.04 LTS compatibility smoke |

Never boot, build in, modify, or delete either base template during an ordinary
build. Run the development workflow through disposable linked clones:

```bash
./release/pve/linux/build.sh
```

The orchestrator transfers the current worktree, builds and tests it in a
Debian clone, retrieves the artifact and logs, then validates that exact
artifact through offline and live smoke in an Ubuntu clone. It promotes output
to `release/bin/` only after both operating-system gates pass.

Successful exact clones are shut down and deleted. The active clone and
available diagnostics are retained on failure. At the beginning of the next
run, only exact unprotected non-template clones named
`proxytools-debian-build-VMID` or `proxytools-ubuntu-validation-VMID` may be
reconciled automatically. Unrelated VMs and protected templates are never
cleanup targets.

Logs remain under `release/.work/pve-linux/logs/{debian,ubuntu}/`. Override
connection defaults without editing the script when necessary:

```bash
PROXYTOOLS_PVE_HOST=root@PVE_HOST \
PROXYTOOLS_PVE_ROOT_KEY=/path/to/pve-root-key \
PROXYTOOLS_PVE_GUEST_KEY=/path/to/proxytools-build-key \
  ./release/pve/linux/build.sh
```

The default host key is `~/.ssh/id_rsa`; the guest key is
`~/.ssh/proxytools-build`. Ephemeral guest host keys stay in ignored build work.

For a publishable candidate, use the explicit clean-snapshot mode:

```bash
./release/pve/linux/build.sh --release
```

It refuses tracked or untracked worktree changes before cleanup, creates one
checksummed `git archive` from `HEAD`, and verifies that same archive in both
guests. Build and provisioning operations share a PVE-side kernel lock; a
contender exits before touching local output or VMs.

### Provision or audit the Linux templates

The clean-host bootstrap is `release/pve/linux/provision-host.sh`. PVE storage
and `vmbr0` networking are site-specific prerequisites and must already be
configured. Copy the script and only the public half of the dedicated guest key
to the host:

```bash
scp release/pve/linux/provision-host.sh root@PVE_HOST:/root/
scp ~/.ssh/proxytools-build.pub root@PVE_HOST:/root/
ssh root@PVE_HOST \
  /root/provision-host.sh \
    --ssh-public-key /root/proxytools-build.pub
```

The bootstrap validates host prerequisites, verifies official cloud-image
checksums, creates and validates protected templates `9000` and `9001`, and
retains failed provisioning state for diagnosis. It never replaces an occupied
VMID or a mismatched cached image automatically.

Audit the existing host without downloading, creating, or changing anything:

```bash
ssh root@PVE_HOST /root/provision-host.sh --check-only
```

Run infrastructure checks explicitly; they are not part of the Python source
test suite:

```bash
./release/pve/linux/tests/test_build.sh
./release/pve/linux/tests/test_provision_host.sh
```

The second command performs a read-only comparison against the configured PVE
host. All Linux PVE code and checks stay under `release/pve/linux/`; Windows PVE
code and checks stay under `release/pve/windows/`. The root `tests/` directory
is only for Python tests of project source.

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
match the expected disposable guest.

## Windows build stage

Windows work is deliberately staged. First create and validate a repeatable
template under `release/pve/windows/`. Do not package or test the project binary
until the template is accepted.

The target build environment is current Windows 11 Enterprise Evaluation;
LTSC 2024 evaluation media has an expired fixed build and must not be used.
Python 3.13 is pinned and Windows 11-only APIs must be avoided. The resulting
x86_64 executable is expected to work on Windows 10, but compatibility must not
be described as validated until the frozen artifact passes a native Windows 10
smoke run.

The Windows stage must reuse the Linux workflow's safety contract: protected
immutable template, disposable clones, retained verified source media, bounded
waits, exact cleanup guards, complete returned logs, and the same clean source
snapshot used for Linux. Its implementation and infrastructure checks belong
only under `release/pve/windows/`.

Release publication and tagging remain deferred until both platform binaries
pass their native gates. Manual TUI acceptance happens before the user declares
a release ready and is not part of release automation.
