# Proxy Tools developer guide

This document describes implementation details for contributors, maintainers, and operators who need to understand the internals. User installation, commands, controls, configuration, and common workflows belong in [README.md](README.md).

## Design goals

Proxy Tools follows a small set of project-level constraints:

- one root entrypoint: `./proxytools`;
- one process per clone;
- one dependency and package manifest: `pyproject.toml`;
- clone-local runtime state that is never committed;
- lightweight `requests` checks for continuous monitoring;
- Selenium only when explicitly requested by the user;
- UI rendering kept separate from the monitoring engine;
- explicit distinction between network reachability and complete acceptance.

The project targets Python 3.10 or newer and uses a `src/` package layout.

## Repository structure

```text
proxytools                  POSIX shell launcher and environment bootstrap
proxytools.conf             strict, commented runtime defaults
pyproject.toml              build metadata, version source, dependencies, entrypoint
README.md                   end-user documentation
DEVELOPERS.md               implementation and contributor documentation
src/proxytools/
  __main__.py               python -m proxytools bridge
  cli.py                    root command dispatch and shared startup lifecycle
  cleanup.py                --clear implementation
  config.py                 config parser and CLI value validation
  paths.py                  clone-local runtime layout and legacy migration
  process_lock.py           advisory per-clone process lock
  geoip.py                  monthly DB-IP download and local lookup reader
  models.py                 shared result records
  http.py                   thread-local requests sessions
  browser.py                disposable interactive browser launcher
  browser_session.py        detached temporary-profile lifecycle helper
  commands/
    list.py                 one-shot orchestration and stdout output
    monitor.py              monitor construction and Textual startup
  sources/
    proxyscrape.py          candidate source adapter
  checking/
    proxy.py                identity, latency, exit IP, and target URL checks
    browser.py              optional Selenium validation
  stability/
    history.py              rolling samples and state transitions
    policy.py               configurable stable-admission rules
  storage/
    sqlite.py               versioned persistence and restoration
  output/
    console.py              Rich stderr progress output
    results.py              list filtering, sorting, and debug formatting
    dashboard.py            monitor state rendering and application lifecycle
    dashboard_widgets.py    filter modals and reusable Textual widgets
tests/                      isolated unittest suite with mocked network access
```

Generated directories are absent from a fresh clone and ignored by Git:

```text
.venv/                      local Python environment
proxydb/                    SQLite database, WAL/SHM, and process lock
geodb/                      local GeoIP database and month marker
```

## Launcher and bootstrap

The root `proxytools` shell script is the supported entrypoint. It resolves its own directory, so commands work regardless of the caller's current directory.

For a working command, the launcher:

1. creates `.venv` when missing;
2. compares `pyproject.toml` with an installation stamp;
3. verifies that required imports are available;
4. runs `pip install -e <project>` when installation or refresh is needed;
5. exports `PYTHONPATH` and `PROXYTOOLS_HOME`;
6. delegates to `python -m proxytools`.

`pyproject.toml` is the only dependency manifest. The project version is read dynamically from `proxytools.__version__`, avoiding a second manually synchronized version string.

Root help is handled before environment creation. `--clear` uses the system Python because it may remove `.venv` while running.

## Command dispatch

`proxytools.cli` exposes two commands:

- `list`, which is also selected when no mode is supplied;
- `monitor`.

Before a real command executes, the dispatcher:

1. acquires the clone lock;
2. ensures the local GeoIP database is usable and current;
3. configures the shared local GeoIP reader;
4. invokes the selected command module.

Help and version do not acquire the process lock. Configuration errors and lock conflicts are converted into concise CLI errors.

## Configuration model

`proxytools.conf` is a complete flat `KEY=value` file. It is parsed with `shlex` as data and is never sourced by a shell.

`RuntimeConfig` contains the typed values used by command construction. The loader rejects:

- unknown keys;
- duplicate keys;
- missing keys;
- malformed assignments;
- invalid types or ranges;
- impossible cross-field combinations such as minimum checks exceeding history size.

Technical tuning belongs in this file. The intentionally small CLI surface contains only options that are useful to change for one invocation.

## Proxy checking pipeline

### Candidate source

`sources.proxyscrape` downloads HTTP, SOCKS4, and SOCKS5 lists. Deduplication uses `(protocol, address)` rather than address alone because one endpoint may support more than one proxy protocol.

### Base identity check

`checking.proxy.check_proxy` sends each configured sample through the proxy to:

```text
https://api.ipify.org?format=json
```

This single HTTPS request provides three signals:

- the proxy can carry an HTTPS request;
- the complete request duration can be measured;
- the public exit IP observed outside the proxy is known.

Latency is the median complete duration across configured samples. Country, city, and coordinates are resolved locally from the exit IP; the identity service is not trusted for geolocation.

### Reachability versus acceptance

These concepts must remain separate throughout the codebase:

- `ProxyResult.reachable` means the base identity request completed and latency was measured;
- `CheckSample.accepted` means the complete health check passed all current criteria.

A reachable result can remain unaccepted because:

- median latency exceeds the configured maximum;
- the configured target URL failed;
- another complete-check policy rejected it.

Latency statistics use all reachable measurements. Success rates and streaks use accepted checks. Do not reintroduce a generic `ok` field: its former double meaning caused UI, persistence, and state-transition inconsistencies.

### Target URL check

When `URL` or `--url` is set, `check_url` performs an additional streamed `requests` call through the same proxy. It does not launch Selenium.

HTTP responses below 400 pass. Monitor and list currently accept 403 because anti-bot pages frequently reject a non-browser client while remaining usable through the interactive browser. Other failures set `failure_reason="url"` while preserving base reachability and measured latency.

### Selenium validation

`list --browser-check` runs only after lightweight identity and target checks pass. It is deliberately serialized to one Chrome instance.

The validator rejects browser network-error pages and main-document HTTP error responses. Visible successful sessions remain open briefly; headless sessions close immediately.

## Monitoring engine

`MonitorEngine` has no Textual dependency. It receives source, checker, policy, and repository adapters and publishes immutable `MonitorSnapshot` records.

### Two work lanes

Long-running monitoring uses independent executors:

- the active lane repeatedly checks known `STABLE` and `PROBATION` proxies;
- the discovery lane processes fresh ProxyScrape candidates;
- a one-worker source executor refreshes candidate lists.

Roughly 20% of configured workers are reserved for active proxies. Each lane receives at least one worker, preventing discovery from blocking probation progress.

An overlap guard prevents the same `(protocol, address)` from running in both lanes simultaneously. Newly successful discovery candidates become eligible for the active lane immediately.

### Snapshots and UI responsiveness

The engine publishes full snapshots at phase boundaries and incremental snapshots while checks complete. Incremental updates contain only changed rows. Waiting snapshots update countdown/status information without forcing the dashboard to rewrite thousands of cells.

The dashboard preserves selection and scroll position, updates only changed cells, rebuilds large filtered views in small event-loop chunks, and sorts only at controlled pass boundaries.

Candidates without any measured latency remain in engine queues but are omitted from visible snapshots.

## Stability model

`ProxyHistory` owns a bounded deque of `CheckSample` values. Each sample records:

- monotonic check time;
- reachability;
- complete acceptance;
- measured latency when available;
- failure reason.

The policy evaluates:

- minimum check count;
- accepted success rate;
- accepted success streak;
- continuous accepted uptime;
- median latency;
- latency jitter.

### Initial admission

A new proxy starts in `PROBATION`. It becomes `STABLE` only when `StabilityPolicy.blockers()` returns no reasons.

### Failure tolerance

A stable proxy retains its state through the configured number of hard failures. The next hard failure resets continuous live time, moves it to `PROBATION`, and begins the degradation grace period.

Continued hard failure for `MONITOR_DEGRADED_AFTER` produces `DEGRADED`. A previously stable proxy returns to `STABLE` after one complete accepted recovery check.

A reachable but slow result is a quality miss. It can block initial stable admission but does not by itself demote an already stable proxy.

### URL failures

A target URL failure is a hard complete-check failure because the user explicitly required access to that destination. Base reachability and latency remain available for diagnostics and statistics.

## SQLite persistence

`StateRepository` uses one SQLite database under `proxydb/`. WAL mode permits safe batched writes and inspection while the monitor is active. The visible companion files are normal SQLite state:

- `proxytools.db`;
- `proxytools.db-wal`;
- `proxytools.db-shm`.

Only useful restart state is retained. `DEGRADED` and never-working candidates are removed rather than accumulated as a graveyard.

The schema contains:

- `proxies` for current state and lifetime aggregates;
- `checks` for recent rolling-history reconstruction;
- `state_transitions` for status changes.

`checks.accepted` and `checks.reachable` intentionally remain separate. Failure reasons are persisted so target failures retain their meaning after restart.

### Schema migrations

SQLite `PRAGMA user_version` identifies the schema version. Migrations in `_migrate_schema()` must be:

- ordered;
- transactional where SQLite permits;
- backward compatible with databases created by released versions;
- covered by a migration test.

Schema version 2 renamed ambiguous `ok` fields to `accepted`, retained `reachable`, and added persisted failure reasons.

Do not silently discard a user's database to simplify a migration.

### Restoration

On startup, recent checks rebuild in-memory rolling history using wall-clock timestamps translated into the current monotonic clock domain.

Long application downtime is not counted as live proxy uptime. Restored status remains visible with `*` until the first fresh check. Failure grace metadata and previous-stable history survive restart.

Detailed checks older than 24 hours are pruned while retained aggregates remain.

## Runtime paths and compatibility migration

`paths.py` resolves all runtime state relative to `PROXYTOOLS_HOME`, not the caller's working directory.

Current layout:

```text
proxydb/proxytools.db
proxydb/proxytools.db-wal
proxydb/proxytools.db-shm
proxydb/proxytools.lock
geodb/geoip.mmdb
geodb/version
```

Legacy root-level database, WAL, SHM, lock, GeoIP, and version files are atomically moved to the new layout when their paths are first accessed. A destination is never overwritten when both old and new files exist.

## Process locking

`ProcessLock` uses Linux `flock`, not file existence. Kernel lock ownership disappears automatically after normal exit or a crash, while the retained JSON file provides PID, command, start time, and database path for diagnostics.

The lock is per clone because it resides under that clone's `proxydb/`. Separate clones can run independently.

## GeoIP lifecycle

`geoip.py` derives the expected DB-IP City Lite filename from the current UTC month. On a missing or outdated local database it:

1. downloads the gzip archive over HTTPS;
2. streams it into a temporary file under `geodb/`;
3. decompresses to another temporary file;
4. opens the result with `maxminddb` to validate it;
5. atomically replaces the active database;
6. writes the month marker.

If an update fails, a valid existing database remains active. With no usable database, proxy checks continue and locations become `Unknown`.

The shared reader is safe for concurrent lookup calls. Attribution must remain visible in user documentation: IP Geolocation by DB-IP, https://db-ip.com.

## Disposable browser sessions

The monitor's `b` action launches a detached helper process so cleanup can outlive the Textual application.

Chrome receives incognito mode, a temporary user-data directory, and a command-line proxy. Firefox receives private mode and a generated profile containing protocol-specific preferences. SOCKS4 and SOCKS5 versions are configured explicitly.

The helper removes its temporary profile after the browser exits. It never reads or modifies the user's normal browser profile.

## Cleanup behavior

`./proxytools --clear` is intentionally destructive only toward known runtime artifacts. It removes environments, generated databases, runtime directories, bytecode, and common build/test caches while preserving source, Git metadata, `.env` files, and arbitrary exports.

Cleanup acquires the same per-clone process lock and refuses to proceed while another working command is running. When adding a new generated artifact, update:

- `.gitignore`;
- `cleanup.py`;
- cleanup tests;
- the user-facing local-data section in `README.md`.

## Testing

Run the standard-library test suite from the repository root:

```bash
./.venv/bin/python -m unittest discover -v
```

Network, browser, filesystem-home, and time-dependent behavior should be mocked. Tests must not depend on live ProxyScrape, DB-IP, identity services, or locally installed browsers.

Useful verification commands:

```bash
find src/proxytools -name '*.py' -print0 \
  | xargs -0 ./.venv/bin/python -m py_compile
sh -n proxytools
git diff --check
```

The most important regression areas are:

- state transitions and failure tolerance;
- reachability versus acceptance;
- SQLite save/restore and schema migration;
- incremental dashboard rendering and selection preservation;
- process locking and cleanup safety;
- runtime-path migration;
- browser profile isolation.

## Dependency changes

Runtime dependencies belong only in `pyproject.toml`. Do not add a manually maintained `requirements.txt`; it creates a second dependency source that can drift.

After changing dependencies, run `./proxytools` once to exercise the real launcher refresh path as well as the test suite.

## Adding a CLI option

The CLI intentionally follows KISS. Before adding a flag, decide whether the value:

- changes user intent for one invocation — a CLI option may be appropriate;
- is a technical tuning default — it belongs in `proxytools.conf`;
- is internal implementation policy — it may not need to be user-configurable.

When a flag is added or changed, update command help, `README.md`, parser-surface tests, and configuration documentation together.

## Contribution checklist

Before handing off a change:

1. keep command modules orchestration-focused;
2. preserve the one-entrypoint workflow;
3. keep stdout machine-friendly in normal `list` mode;
4. avoid blocking the Textual event loop;
5. preserve existing clone-local state through migrations;
6. update cleanup for new generated files;
7. update user and developer documentation at the correct level;
8. run tests, Python compilation, shell syntax validation, and `git diff --check`.
