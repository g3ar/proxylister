# Proxy Tools developer guide

This guide is the shortest path from a fresh clone to a safe, tested change.
User installation, commands, keyboard controls, configuration examples, and
troubleshooting belong in [README.md](README.md). This file explains how the
code fits together, where to add behavior, and which invariants must survive a
change.

Proxy Tools targets Python 3.10 or newer, uses a `src/` package layout, and has
one supported entrypoint:

```bash
./proxytools [list|monitor] [options]
```

## First 10 minutes

From the repository root:

```bash
git status --short --branch
./proxytools --help
./proxytools list --help
./proxytools monitor --help
./.venv/bin/python -m unittest discover -v
```

The first real command creates `.venv` and installs the project. If `.venv`
does not exist yet, run `./proxytools --version` or another normal command
before invoking its Python directly.

Before editing:

1. preserve existing modified and untracked files;
2. find the relevant command, implementation, and tests with `rg`;
3. read the code path end to end rather than patching the first matching
   function;
4. add or update a regression test for behavior changes;
5. keep the change within the existing architecture unless a new boundary is
   clearly necessary.

The project deliberately favors small, removable designs. Do not add another
entrypoint, dependency manifest, generic result flag, background service, or
configuration layer for a hypothetical future need.

## Mental model

Both commands share startup, discovery, and lightweight proxy checking:

```text
./proxytools
    │
    ├─ launcher: create/refresh .venv
    └─ cli.py: lock clone, prepare GeoIP, dispatch command
         │
         ├─ list.py
         │    └─ fetch candidates → check proxies → filter/sort → stdout
         │
         └─ monitor.py
              └─ MonitorEngine → snapshots → Textual dashboard
                    │
                    ├─ StabilityPolicy classifies history
                    └─ StateRepository saves useful restart state
```

The most important boundary is between the monitoring engine and the TUI.
`MonitorEngine` knows nothing about Textual. It publishes immutable snapshots;
the dashboard renders them and sends user actions to existing services.

## Where to make a change

| Goal | Start here | Usually update |
|------|------------|----------------|
| Change root dispatch or shared startup | `src/proxytools/cli.py` | `tests/test_cli.py` |
| Change `list` behavior or options | `src/proxytools/commands/list.py` | CLI and proxy-library tests |
| Change monitor construction or options | `src/proxytools/commands/monitor.py` | CLI tests |
| Change candidate discovery | `src/proxytools/sources/proxyscrape.py` | `tests/test_proxylib.py` |
| Change HTTP proxy validation | `src/proxytools/checking/proxy.py` | proxy and monitoring tests |
| Change Selenium validation | `src/proxytools/checking/browser.py` | browser tests |
| Change stability rules | `src/proxytools/stability/` | `tests/test_cli_helpers.py` |
| Change monitor scheduling or snapshots | `src/proxytools/monitoring.py` | `tests/test_monitoring.py` |
| Change the TUI table or actions | `src/proxytools/output/dashboard.py` | `tests/test_dashboard.py` |
| Add a TUI modal or reusable widget | `src/proxytools/output/dashboard_widgets.py` | `tests/test_dashboard.py` |
| Change saved monitor state | `src/proxytools/storage/sqlite.py` | `tests/test_persistence.py` |
| Change runtime paths or cleanup | `paths.py`, `cleanup.py` | path and cleanup tests |
| Change configuration | `config.py`, `proxytools.conf` | config and CLI tests |
| Change dependencies or version metadata | `pyproject.toml` | launcher smoke test |
| Change About text, credits, or build date | `src/proxytools/about.py` | CLI and dashboard tests |

## Repository map

```text
proxytools                  POSIX launcher and environment bootstrap
proxytools.conf             strict runtime defaults
pyproject.toml              only project/dependency manifest
src/proxytools/
  cli.py                    root dispatch and shared startup lifecycle
  cleanup.py                --clear implementation
  config.py                 config parsing and value validation
  paths.py                  clone-local paths and legacy migration
  process_lock.py           advisory per-clone lock
  geoip.py                  DB-IP download and local lookup
  models.py                 shared result records
  http.py                   bounded Requests session helpers
  browser.py                interactive browser launcher
  browser_session.py        detached temporary-profile helper
  about.py                  shared CLI/TUI identity and contributor credits
  commands/
    list.py                 one-shot orchestration and output
    monitor.py              monitor construction and TUI startup
  sources/proxyscrape.py    candidate source adapter
  checking/
    proxy.py                identity, latency, exit IP, and URL checks
    browser.py              optional Selenium validation
  stability/
    history.py              rolling samples and transitions
    policy.py               stable-admission policy
  storage/sqlite.py         versioned monitor persistence
  output/
    console.py              Rich stderr progress
    results.py              list formatting, filtering, and sorting
    dashboard.py            Textual rendering and app lifecycle
    dashboard_widgets.py    filter and analytics modals
tests/                      offline standard-library unittest suite
```

Generated state is per clone and ignored by Git:

```text
.venv/
proxydb/                    SQLite database, WAL/SHM, and lock metadata
geodb/                      GeoIP database and version marker
```

## Core data semantics

Two facts that sound similar have deliberately different meanings:

- `ProxyResult.reachable`: the HTTPS identity request succeeded and latency
  was measured;
- `CheckSample.accepted`: the complete configured health check passed.

A proxy can be reachable but not accepted because its median latency is too
high or the configured target URL failed. Latency statistics use reachable
measurements; success rate, streak, and healthy time use accepted checks.

Do not add a generic `ok` property or database column. It previously meant
both concepts in different places and caused incorrect state transitions,
persistence, and UI output.

Candidate identity is `(protocol, address)`, not address alone. The same
endpoint may genuinely support HTTP, SOCKS4, and SOCKS5.

## The checking pipeline

`sources.proxyscrape` supplies public candidates. For every configured sample,
`checking.proxy.check_proxy` sends an HTTPS request through the candidate to:

```text
https://api.ipify.org?format=json
```

That request establishes reachability, measures the complete request duration,
and returns the observed public exit IP. Latency is the median across samples.
Country, city, and coordinates are resolved locally from that exit IP; source
metadata is never authoritative for the browser-facing location.

If `URL` or `--url` is configured, `check_url` makes one additional lightweight
request through the proxy. Responses below 400 pass. HTTP 403 also passes
because anti-bot sites may reject `requests` while remaining usable in a real
browser. Other HTTP and network failures preserve reachability and latency but
make the complete sample unaccepted with `failure_reason="url"`.

Continuous URL checking must stay in Requests. Selenium is used only by
explicit `list --browser-check`; it runs after lightweight checks and is
serialized to one Chrome instance.

### Requests lifecycle invariant

Each logical proxy check must create and close a short-lived `proxy_session`,
including every response, on success and on every error path. Requests caches
a connection manager for each proxy URL; feeding unlimited public proxies to a
shared session eventually exhausts file descriptors. Thread-local sessions are
appropriate only for bounded upstream services such as the candidate source.

## `list` flow

`commands/list.py` owns one-shot orchestration:

1. fetch and deduplicate candidates;
2. run lightweight checks concurrently;
3. optionally run explicit Selenium validation;
4. filter and sort accepted results;
5. write progress to stderr and results to stdout.

Normal stdout is intentionally machine-friendly: one connection string per
line. Keep diagnostics behind `list --debug`, and never mix progress text into
stdout because users pipe and redirect it.

## `monitor` flow

`MonitorEngine` owns scheduling and state; `ProxyMonitorApp` owns presentation.
The engine receives source, checker, policy, and repository adapters, making it
testable without Textual or live network access.

Monitoring uses independent work lanes:

- the active lane rechecks known `STABLE` and `PROBATION` proxies;
- the discovery lane checks newly fetched candidates;
- a one-worker source executor refreshes candidate lists.

About 20% of configured workers are reserved for the active lane, and each lane
gets at least one worker. This prevents a large discovery batch from stopping
known candidates from progressing. An overlap guard prevents the same proxy
from running in both lanes at once.

The engine publishes immutable `MonitorSnapshot` values:

- full snapshots establish or reconcile the visible set at phase boundaries;
- incremental snapshots contain only changed rows while checks finish;
- waiting snapshots update countdown and status without rewriting the table.

Candidates without measured latency stay in backend queues but are omitted
from visible snapshots.

### TUI rules

Never perform network, SQLite, Selenium, or a large synchronous table rebuild
on the Textual event loop.

The dashboard updates changed cells, rebuilds large filtered views in chunks,
preserves selection and scroll position, and sorts only at controlled pass
boundaries. Keep those properties when changing rendering.

The main table intentionally has one compact layout: State, Country, Median,
Alive, and Connection. Detailed proxy analytics belong in the `Enter` modal,
not additional permanent columns or a separate debug mode. The `y` action uses
Textual's OSC 52 support; do not add platform clipboard dependencies.

## Stability model

`ProxyHistory` stores a bounded deque of `CheckSample` values. Each sample has
a monotonic timestamp, reachability, acceptance, optional latency, and failure
reason. `StabilityPolicy.blockers()` evaluates check count, accepted success
rate, success streak, continuous healthy time, median latency, and jitter.

The only states are:

```text
STABLE → PROBATION → DEGRADED
```

New proxies begin in `PROBATION` and enter `STABLE` only when no admission
blockers remain.

A stable proxy tolerates the configured number of consecutive hard failures.
The next failure resets continuous healthy time, moves it to `PROBATION`, and
starts the degradation grace period. Continuous hard failure for
`MONITOR_DEGRADED_AFTER` moves it to `DEGRADED`. One accepted recovery check
may restore a proxy that was previously stable.

Latency follows a separate rule:

- `STABLE` always requires rolling median `< MAX_LATENCY`;
- one isolated slow sample should not cause churn;
- sustained slow median moves `STABLE` to `PROBATION`;
- a reachable slow proxy never becomes `DEGRADED` from latency alone;
- it returns to `STABLE` when the rolling median is acceptable again.

Target URL failure is a hard complete-check failure because the user explicitly
required that destination. It still preserves base reachability and latency.

When changing stability behavior, test the full transition sequence, not just
the final state. Include timestamps and consecutive samples that demonstrate
the intended tolerance, grace period, and recovery.

## Persistence and restoration

`StateRepository` stores useful `STABLE` and `PROBATION` restart state in
`proxydb/proxytools.db`. WAL mode produces normal `-wal` and `-shm` companion
files. `DEGRADED` and never-working candidates are removed rather than retained
as an unlimited history of dead proxies.

The schema contains:

- `proxies`: current state and lifetime aggregates;
- `checks`: recent rolling history;
- `state_transitions`: state changes.

`checks.accepted` and `checks.reachable` must remain separate. Failure reasons
are persisted so URL failures retain their meaning after restart.

On restoration, wall-clock timestamps are translated into the current
monotonic clock domain. Application downtime does not count as live proxy
uptime. A restored row carries `*` until its first fresh check, and persisted
`STABLE` state is normalized against the current latency limit. Checks older
than 24 hours are pruned while lifetime aggregates remain.

### Schema changes

`PRAGMA user_version` selects ordered migrations in `_migrate_schema()`. A
migration must preserve databases created by earlier releases, be transactional
where SQLite permits, and have a regression test. Never delete or recreate a
user's database merely to make a schema change easier.

## Shared startup and runtime services

### Launcher and command dispatch

The root `proxytools` script resolves the checkout directory, creates or
refreshes `.venv`, installs the editable project from `pyproject.toml`, exports
`PYTHONPATH` and `PROXYTOOLS_HOME`, and delegates to `python -m proxytools`.
`pyproject.toml` is the only dependency and project manifest; the package
version is read dynamically from `proxytools.__version__`.

`about.py` is the single source for the name, one-sentence description,
contributors, build date, and formatted About text used by both `--about` and
the TUI's `F1` modal. Add known contributors explicitly to `AUTHORS`; never
infer or invent model names. Until a release build pipeline supplies a precise
date, `BUILD_DATE` intentionally contains only the project build year.

Root help runs before environment creation. `--clear` uses the system Python
because it may delete `.venv` while running.

For a real command, `cli.py` acquires the clone lock, prepares the local GeoIP
database, configures the reader, and dispatches `list` or `monitor`. Help and
version do not acquire the lock.

### Configuration

`proxytools.conf` is a complete flat `KEY=value` document parsed as data with
`shlex`; it is never sourced as shell code. `RuntimeConfig` contains typed
values. The parser rejects unknown or duplicate keys, missing values, malformed
assignments, invalid ranges, and impossible cross-field combinations.

Technical defaults belong in the config file. Add a CLI option only when users
reasonably need to change intent for one invocation. When changing the public
surface, update command help, README examples, and parser-surface tests.

### Runtime paths and locking

`paths.py` resolves state from `PROXYTOOLS_HOME`, never from the caller's current
directory. Legacy root-level database, lock, GeoIP, and version files are moved
atomically into `proxydb/` or `geodb/` without overwriting an existing target.

`ProcessLock` uses Linux `flock`, not lock-file existence. Kernel ownership is
released after exit or a crash; retained JSON is only diagnostic metadata. The
lock is per clone, so separate clones may run simultaneously.

### GeoIP

`geoip.py` selects the current monthly DB-IP City Lite archive, downloads and
decompresses it through temporary files, validates it with `maxminddb`, then
atomically replaces the active database and version marker. A failed update
keeps a valid old database. Without a usable database, checks continue with
location `Unknown`.

The shared reader supports concurrent lookups. Keep DB-IP attribution visible
in user documentation.

### Disposable browser sessions

The monitor's `b` action launches a detached helper so profile cleanup can
outlive the TUI. Chrome/Chromium receives incognito mode and a temporary user
data directory. Firefox receives private mode and a generated profile with
protocol-specific settings. SOCKS4 and SOCKS5 versions are explicit. The
helper never reads or changes the user's normal browser profile.

### Cleanup

`./proxytools --clear` removes only known generated artifacts and refuses to
run while another command owns the clone lock. Source, Git metadata, `.env`,
and arbitrary user exports are preserved.

When adding generated state, update all four places together:

1. `.gitignore`;
2. `cleanup.py`;
3. cleanup tests;
4. the local-data section in `README.md`.

## Common extension recipes

### Change a proxy check

1. Decide whether the result affects reachability, acceptance, or both.
2. Preserve a measured latency whenever base identity succeeded.
3. Set a specific failure reason for complete-check failures.
4. Close responses and the short-lived proxy session on every path.
5. Add offline tests for success, rejection, and exception cleanup.

### Change stability policy

1. Add the rule to `StabilityPolicy`, not the TUI or command module.
2. Decide how it affects new admission, stable tolerance, degradation, and
   recovery.
3. Test sequences of samples and time, including restart normalization if the
   rule depends on persisted history.
4. Expose a configuration value only if operators genuinely need to tune it.

### Change the TUI

1. Keep engine data and scheduling independent from Textual.
2. Put reusable modals and interaction widgets in `dashboard_widgets.py`.
3. Keep expensive work off the event loop.
4. Preserve row identity, selection, scroll position, and controlled sorting.
5. Test the action through Textual's async test pilot.
6. Update the monitor section in `README.md` when user interaction changes.

### Change persisted state

1. Add an ordered schema migration rather than replacing the database.
2. Preserve the distinction between accepted and reachable.
3. Test migration from the previous schema and current round-trip behavior.
4. Confirm restored histories preserve grace periods and do not count downtime
   as healthy time.

### Add a dependency

Add it only to `pyproject.toml`. Do not create `requirements.txt`. Run the real
launcher once so its refresh/install path is exercised in addition to tests.

## Testing and validation

Tests use standard-library `unittest` and must remain offline and deterministic.
Mock ProxyScrape, DB-IP, identity services, target sites, browsers, clocks, and
filesystem homes as appropriate.

For a normal Python change, run from the repository root:

```bash
./.venv/bin/python -m unittest discover -v
find src/proxytools -name '*.py' -print0 \
  | xargs -0 ./.venv/bin/python -m py_compile
sh -n proxytools
git diff --check
git status --short --branch
```

High-risk regression areas are:

- reachability versus complete acceptance;
- stability tolerance, grace, latency, and recovery;
- SQLite migration and restoration;
- Requests resource cleanup;
- active/discovery lane independence;
- incremental TUI rendering and selection preservation;
- clone locking, cleanup, and legacy path migration;
- disposable browser profile isolation.

## Handoff checklist

Before handing a change back:

1. inspect the exact diff and preserve unrelated worktree changes;
2. confirm behavior at the command or TUI boundary, not only in a unit helper;
3. run the full validation block above;
4. update `README.md` for user-visible behavior and this guide for architectural
   or contributor-workflow changes;
5. report behavior changed, tests actually run, remaining risks, and whether a
   commit or push was performed.

Do not commit or push unless the user explicitly authorizes it. Do not rebase,
amend, force-push, or discard an existing worktree change as routine cleanup.
