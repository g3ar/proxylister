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
tested before commit. It removes only its previous
`release/.work/local-linux/`, then:

1. creates a fresh release-only Python virtual environment;
2. installs the current project and pinned PyInstaller;
3. runs the complete unit test suite;
4. compiles all project Python modules and checks the POSIX scripts;
5. builds the one-file executable from
   `release/pyinstaller/proxytools.spec`;
6. writes an environment and checksum manifest;
7. invokes `release/linux/smoke.sh` against the built executable.

Network access to PyPI is required while the fresh environment is populated.
The ordinary development `.venv` is neither read nor modified.

## Outputs

Generated output is ignored by Git:

```text
release/.work/local-linux/
  artifacts/
    proxytools
    README.md
    MANIFEST.txt
  logs/
    build.log
    smoke.log
  pyinstaller/
  venv/
```

The copied `README.md` is the project root document for end users. Build
instructions do not belong in that file.

`MANIFEST.txt` records the source commit, clean/dirty worktree state, build UTC
time, operating system, glibc, Python, PyInstaller, and artifact checksums. An
artifact whose manifest says `source_tree=dirty` is for development testing
only.

The build script writes detailed output to `logs/build.log` and prints the
artifact, manifest, and log paths after success. On failure, inspect the end of
that log first.

## Frozen smoke coverage

The smoke script copies the executable into a new temporary directory and
runs it with an empty environment and no Python in `PATH`. It currently checks:

- version and project information;
- root, `list`, and `monitor` help;
- inclusion of both dynamically imported command modules;
- creation and preservation of external `proxytools.conf` beside the binary;
- `--clear` without source-only `.venv` state;
- absence of unexpected `proxydb/` and `geodb/` after help/cleanup checks;
- a clear configuration error when the executable directory is not writable.

These checks are offline and deterministic. Live proxy, GeoIP, browser, full
monitor PTY, and concurrent-process checks remain future frozen smoke work.

To rerun smoke tests against an existing artifact without rebuilding:

```bash
./release/linux/smoke.sh \
  release/.work/local-linux/artifacts/proxytools
```

## Release limitation

PyInstaller is pinned by the build script, but the full resolved Python
dependency set is not locked yet. Therefore this workflow proves that the
current source can be frozen and exercised locally; it is not yet the final
reproducible release pipeline.
