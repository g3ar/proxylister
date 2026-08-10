# Local Linux build

This is the implemented contributor workflow for building and testing the
Linux x86_64 one-file executable on the current machine. It is the default
place to develop and validate packaging changes.

Ordinary code iteration should continue to use focused tests and the full
source validation documented in `DEVELOPERS.md`. A frozen build is most useful
for packaging, startup, bundled-resource, runtime-path, and release-related
changes; it need not slow down every small edit.

## Run the build

From any directory inside the checkout, run:

```bash
./release/linux/build.sh
```

The script intentionally accepts a dirty worktree so packaging changes can be
tested before commit. At startup it removes its previous
`release/.work/local-linux/` and `release/bin/`, so an obsolete binary can never
be mistaken for the result of the current attempt. It then:

1. creates a fresh release-only Python virtual environment;
2. installs the current project and build tooling under the exact versions in
   `release/linux/constraints.txt`;
3. rejects any resolved package set that differs from those constraints;
4. runs the complete unit test suite;
5. compiles all project Python modules and checks the POSIX scripts;
6. builds the one-file executable from
   `release/pyinstaller/proxytools.spec`;
7. writes an environment manifest and separate checksum file;
8. invokes `release/linux/smoke.sh` against the built executable;
9. only after success, promotes the complete artifact set to `release/bin/`.

Network access to PyPI is required while the fresh environment is populated.
The ordinary development `.venv` is neither read nor modified.

## Working data and final output

Both locations are generated and ignored by Git. `.work` is intentionally
retained after the build because its environment and logs are needed to
diagnose failures:

```text
release/.work/local-linux/
  logs/
    build.log
    smoke.log
  pyinstaller/
  venv/
```

During a failed attempt, partial or completed build output may also remain in
`.work/local-linux/artifacts/` for diagnosis.

After a successful build, the convenient manual-test copy is:

```text
release/bin/
  proxytools
  README.md
  LICENSE
  MANIFEST.txt
  SHA256SUMS
```

`release/bin/` represents only the current successful attempt. The old
directory is deleted before every build; after deterministic smoke tests pass,
the completed artifact directory is moved there rather than duplicated. If a
build fails, inspect `.work`; the absence of `release/bin/` is deliberate.

The copied `README.md` is the project root document for end users. Build
instructions do not belong in that file.

`MANIFEST.txt` records the artifact name, project version, source commit,
clean/dirty worktree state, build UTC time, operating system, architecture,
glibc, Python, pip, PyInstaller, and the constraints-file checksum.
`SHA256SUMS` covers the executable, user `README.md`, MIT `LICENSE`, and
manifest. The binary itself reports its embedded application version through
`./proxytools --version` and its exact build time and source commit through
`./proxytools --about`; version and architecture do not belong in its filename. An artifact
whose manifest says `source_tree=dirty` is for development testing only.

The build script writes detailed output to `logs/build.log` and prints the
artifact, manifest, and log paths after success. On failure, inspect the end of
that log first.

## Frozen smoke coverage

The smoke script copies the executable into a new temporary directory and
runs it with an empty environment and no Python in `PATH`. It currently checks:

- version and project information;
- root, `list`, and `monitor` help;
- inclusion of both dynamically imported command modules;
- creation and preservation of an edited external `proxytools.conf`;
- refusal to clear state while a real kernel lock is held;
- cleanup of generated `proxydb/` and `geodb/` state without source-only
  `.venv` state;
- a clear configuration error when the executable directory is not writable.

These checks are offline and deterministic. They run automatically after every
successful build.

To rerun smoke tests against an existing artifact without rebuilding:

```bash
./release/linux/smoke.sh \
  release/bin/proxytools
```

## Optional live smoke

Network-dependent checks are separate so an external outage cannot fail the
deterministic build gate:

```bash
./release/linux/smoke-live.sh \
  release/bin/proxytools
```

This bounded check downloads/opens the GeoIP database, completes a real `list`
run, then starts `monitor` in a PTY and exits through its `q` action. Default
timeouts are 300 seconds for `list` and 60 seconds for `monitor`; override them
with `PROXYTOOLS_LIVE_LIST_TIMEOUT` and `PROXYTOOLS_LIVE_MONITOR_TIMEOUT`.
The complete list and monitor output remains in
`release/.work/local-linux/logs/live-list.log` and `live-monitor.log`, including
after a failed check. A later build or live-smoke start replaces its own old
diagnostics; no failure cleanup removes the current attempt.

The live test is diagnostic and release-maintainer-facing, not part of normal
contributor iteration. Interactive browser validation remains manual because
it requires a usable selected proxy and an installed external browser.

## Place in the release pipeline

The local artifact contract and Linux dependency set are fixed. Local builds
remain the normal contributor workflow; publishable builds additionally require
a clean release worktree and the implemented isolated PVE orchestration in
`BUILD_REMOTE.md`. Publishing still waits for the separate Windows stage.
