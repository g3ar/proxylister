# Proxy Tools build overview

Proxy Tools has a primary local developer workflow and an additional remote
release-maintainer workflow. They use the same committed PyInstaller definition
and smoke tests but serve different audiences:

- [BUILD_LOCAL.md](BUILD_LOCAL.md) is the normal contributor path. It builds and
  tests the current worktree on the contributor's own machine.
- [BUILD_REMOTE.md](BUILD_REMOTE.md) is an optional maintainer layer for
  isolated release builds on the project's PVE server. Contributors do not
  need access to it.

Build documentation is for maintainers. End users follow `README.md`, either
from a normal `git clone` checkout or beside a downloaded standalone binary.
The remote lab is not a requirement for a code contribution or pull request.

## Current status

| Capability | Status |
|---|---|
| Local Linux source tests | Implemented |
| Local Linux one-file build | Implemented |
| Local frozen smoke tests | Implemented |
| Debian 13 PVE template | Provisioned and validated |
| Automated remote Linux build | Not implemented |
| Frozen dependency lock | Not implemented |
| Windows template and build | Deferred to a separate later stage |
| Release publishing automation | Not implemented |

Do not describe a planned step as operational. When a stage becomes usable,
update this table and its dedicated runbook in the same change.

## Shared release rules

A local dirty-worktree build is useful evidence during development but is not
publishable. A release build must start from one clean release worktree and
record the exact commit. All platform builders must receive the same source
snapshot, including the committed build and test scripts.

Each native build must:

1. create a fresh build environment;
2. run the complete source tests and validation;
3. build from the committed PyInstaller definition;
4. test the frozen executable without the source tree or build environment in
   `PATH`;
5. return the executable, user-facing `README.md`, manifest, checksums, and
   complete logs.

Generated environments, VM images, credentials, artifacts, and logs are local
state and must never be committed. `pyproject.toml` remains the authoritative
project manifest. A future release constraints file may pin resolved build
inputs but must not become a second hand-maintained runtime manifest.

The supported Linux binary baseline is current stable/LTS distributions. Older
systems may use the source workflow documented in `README.md`.

Windows work is intentionally out of scope until the remote Linux workflow is
complete. When activated, it gets its own runbook rather than being mixed into
either Linux document.
