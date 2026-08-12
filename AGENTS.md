# Proxy Tools — local agent handoff

This file is the shared starting context for new Codex chats working on this
repository. It is versioned with the project so the same operational context is
available after a fresh clone or on another machine. Read it before making
changes, then inspect the current worktree because this file does not replace
the code, tests, `README.md`, `DEVELOPERS.md`, or `BUILD.md` as sources of
truth.

## Start every new chat here

1. Run `git status --short --branch` and preserve all existing user changes.
2. Read the relevant sections of `README.md` and `DEVELOPERS.md`; read
   `BUILD.md` before standalone build, release, or PVE work.
3. Use `rg` / `rg --files` for code and file discovery.
4. Inspect the implementation and tests before proposing a fix.
5. Do not run cleanup, reset, checkout, rebase, amend, revert, force-push, or
   other destructive Git commands.
6. Use `apply_patch` for source and documentation edits.
7. After code changes, run the full test and validation set listed below.

The normal repository root is `/home/gear/wrk/proxylister`, but never hard-code
that path into project code.

## Project in one paragraph

Proxy Tools discovers public HTTP, SOCKS4, and SOCKS5 proxies, validates them
through real HTTPS requests, measures latency, resolves the observed exit IP
through a locally downloaded GeoIP database, and monitors useful candidates in
a Textual terminal UI until they qualify as stable. Selenium is installed with
the project but used only for explicit browser validation or an interactive
disposable browser session.

## Design principle: KISS

Follow KISS ("Keep It Simple, Stupid") wherever doing so does not compromise
correctness, safety, or the agreed user experience. Prefer the smallest design
that completely solves the current problem and remains easy to explain,
operate, test, and remove.

In practice:

- do not add modes, CLI flags, configuration keys, abstractions, background
  services, dependencies, or infrastructure for hypothetical future needs;
- keep one authoritative representation of each concept: one entrypoint, one
  dependency manifest, one configuration model, and one explicit state model;
- prefer straightforward code and data flow over clever indirection;
- reuse an existing mechanism when it already expresses the requirement
  clearly, but do not force unrelated meanings into one field or flag;
- separate components only where the boundary materially improves correctness,
  testability, responsiveness, or platform support;
- optimize after observing a real problem and measuring or reproducing it;
- remove obsolete options, compatibility paths, and dead code once they are no
  longer required and removal is safe;
- document unavoidable complexity and the concrete reason it exists.

KISS does not mean hiding errors, weakening validation, collapsing distinct
semantics such as `reachable` and `accepted`, or implementing a quick patch that
creates ambiguous behavior. Simplicity is judged across the whole user and
maintenance workflow, not merely by the number of lines changed.

## Supported user interface

There is one root entrypoint:

```bash
./proxytools [mode] [options]
```

Modes:

- `list` — one-shot discovery and output; this is the default mode;
- `monitor` — continuous checking with an interactive Textual dashboard.

Keep the CLI small. The intentional public options are:

- mode selection;
- `--help`;
- `--version`;
- `--about`;
- `--clear`;
- `--debug` for `list` output;
- `--url`;
- `--max-latency`;
- `--browser-check` where applicable;
- `--headless` where applicable.

Technical defaults belong in the commented plain-text `proxytools.conf`, not
in an expanding collection of CLI flags. The root launcher bootstraps a local
`.venv`; `pyproject.toml` is the only dependency/project manifest. Do not add a
second `requirements.txt`.

Monitor controls currently include:

- arrows — select and scroll;
- `Esc` — clear the current selection without moving the viewport;
- `F1` — show project information and contributor credits;
- `Enter` — show detailed analytics for the selected proxy;
- double-click — select a proxy and show its detailed analytics;
- `s` — choose states;
- `p` — choose protocols;
- `c` — choose country;
- `b` — open the configured URL through the selected proxy in an isolated
  private browser session and release the table selection;
- `y` — copy the selected connection string with OSC 52;
- `q` or `Ctrl+C` — display live shutdown progress, finish bounded active work,
  and exit through the same graceful path.

Do not reintroduce removed controls such as `d` or `r`. The TUI stays compact:
State, Country, Median, Alive, and Connection. City, Exit IP, blockers,
detailed counters, and other proxy analytics are available through `Enter`;
the monitor has no separate debug mode. Candidates without measured latency
remain in backend queues but must not appear in the table.

## TUI interaction and visual contract

The monitor is a user-facing terminal application, not a developer dashboard.
Keyboard and mouse are equal control paths and must expose the same core
workflow without making either path awkward. Prefer direct, predictable
interaction over framework defaults, hidden state changes, animation, or
decorative complexity.

Preserve these interaction invariants:

- selection is optional and the monitor starts with no proxy selected;
- snapshots, filtering, and background resorts never create selection or
  restore a previously released proxy key;
- without selection, the viewport belongs to the user and background updates
  must not scroll it;
- a click anywhere across a proxy's complete visual row selects it;
- clicking another row moves selection, clicking the selected row again clears
  it, and double-clicking a row performs the same details action as `Enter`;
- `Esc` clears table selection without moving the viewport;
- clicks on empty table space or outside the table clear selection; outside
  clicks continue to the control that received them instead of being consumed;
- `Enter`, `b`, and `y` act only on an explicitly selected proxy;
- `b` captures the selected proxy, releases selection, preserves the viewport,
  and then opens the browser so background monitoring cannot drag the table
  while the terminal is covered;
- proxy details close with `Enter`, `Esc`, or a click on their shaded backdrop;
  clicks inside the modal do not close it or reach the table;
- mouse hitboxes follow visible components and rows, not merely rendered text;
- mouse interaction must not add delays, timers, double scrolling, or hover-led
  state changes;
- the footer is one responsive row of indivisible
  `<hotkey> <description>` blocks distributed across the terminal width;
  essential actions survive narrow layouts while secondary hints disappear;
- live updates must not block the event loop, steal focus, move selection, or
  make keyboard and mouse input lag;
- each UI bug fix gets a regression test for the complete user interaction,
  including selection and viewport effects where relevant.

Use these established terminal applications as design references. Borrow the
specific quality listed here, not their entire layout or command model:

| Reference | Use as guidance for | Do not copy |
|-----------|---------------------|-------------|
| `mc` | Full-width hotkey footer, clear modals, generous hitboxes, discoverability | Two-pane file-manager structure |
| `htop` | Compact live tables, keyboard/mouse parity, updates that respect navigation | Excess permanent counters |
| `less` | User-owned viewport and a normal no-selection state | Its command language |
| `tig` | Dense list/details workflow and returning without losing list context | Git-specific hierarchy |
| `vim` | Predictable navigation, explicit focus, and `Esc` as a safe neutral action | Modal editing and complex key grammar |
| `btop` | Cohesive styling, spacing, visual hierarchy, responsive live layout, and useful mouse targets | Decorative graphs, animation, or information overload without a concrete need |

Standard Textual widgets are implementation tools, not visual or interaction
requirements. In particular, do not fall back to the stock Textual footer,
web-style hover effects, automatic selection, gratuitous notifications,
input-delaying animation, or IDE-like multi-pane complexity. Do not imitate
any reference pixel for pixel; keep Proxy Tools compact and task-specific.

## Architecture

The package uses a `src/` layout:

```text
proxytools                         root POSIX launcher
proxytools.conf                    runtime defaults
src/proxytools/cli.py              root dispatch/lifecycle
src/proxytools/commands/           list and monitor orchestration
src/proxytools/checking/           lightweight and Selenium checks
src/proxytools/stability/          history and state policy
src/proxytools/storage/sqlite.py   persisted monitor state
src/proxytools/output/             stdout/Rich/Textual presentation
src/proxytools/monitoring.py       UI-independent monitoring engine
src/proxytools/geoip.py            DB-IP download and local lookup
src/proxytools/browser*.py         disposable interactive sessions
tests/                             mocked, offline unittest suite
```

Maintain separation between the monitoring engine and Textual UI. Never block
the Textual event loop with network, database, Selenium, or large synchronous
table rebuild work.

The monitor has two independent work lanes:

- active lane for known `STABLE` and `PROBATION` proxies;
- discovery lane for new ProxyScrape candidates.

Known candidates must continue progressing while discovery is busy. Rows are
updated incrementally and reordered only at controlled active-pass boundaries
so selection and scroll position do not jump. Sorting is state group first
(`STABLE`, `PROBATION`, `DEGRADED`), then latency inside each group. A restored
`*` is a marker, never a separate state.

## Checking semantics

Keep these concepts explicit and separate:

- `ProxyResult.reachable` — the HTTPS identity request succeeded and latency
  was measured;
- `CheckSample.accepted` — the entire configured health check passed.

Do not add a generic `ok` field. Its previous double meaning caused UI,
persistence, and transition bugs.

The base identity check uses `requests` through the proxy and obtains the exit
IP from the external identity endpoint. Country/city are resolved locally from
that exit IP. ProxyScrape metadata is not authoritative for browser exit
location.

`list` writes selected plain connection strings to stdout and atomically to
`working_proxies.txt` beside the launcher or frozen executable. A normal
`Ctrl+C` must defer interruption while Rich/executor locks are active, complete
bounded shutdown, and save valid results collected so far without displaying
`release unlocked lock`.

When `--url` or config `URL` is present, check it with lightweight `requests`
through every proxy. Do not invoke Selenium for continuous URL checks. HTTP 403
is accepted in monitor/list URL reachability because anti-bot sites may remain
usable in a browser; other HTTP errors fail the complete check.

`--browser-check` is explicit Selenium validation in `list`. The monitor's `b`
action launches Chrome or Firefox with a temporary isolated profile and must
not touch the user's primary browser profile.

Every proxy check must use a short-lived Requests session and close responses
and sessions on all paths. A shared Requests session fed unlimited proxy URLs
caches proxy managers and previously caused `OSError: [Errno 24] Too many open
files` after long monitor runs.

## Stability rules

The only proxy states are:

```text
STABLE -> PROBATION -> DEGRADED
```

Initial `STABLE` admission uses the configured minimum checks, accepted success
rate, success streak, observed live time, rolling median latency, and jitter.

Failure handling is deliberately tolerant:

- a stable proxy retains `STABLE` through the configured number of consecutive
  hard failures;
- after tolerance is exhausted it moves to `PROBATION` and starts the failure
  grace timer;
- only continuous hard failure for `MONITOR_DEGRADED_AFTER` moves it to
  `DEGRADED`;
- an accepted recovery check may restore a formerly stable proxy without
  repeating its entire initial admission period.

Latency has a separate invariant:

- `STABLE` always requires rolling median `< MAX_LATENCY`;
- one isolated slow sample should not cause state churn;
- sustained rolling median `>= MAX_LATENCY` moves `STABLE` to `PROBATION`;
- a reachable slow proxy never becomes `DEGRADED` because of latency alone;
- it returns to `STABLE` after its rolling median becomes acceptable;
- persisted `STABLE` state is normalized against the current latency limit on
  restoration.

Do not make stability aggressive again by resetting uptime or dropping stable
status after one transient network or latency miss.

## Local data and SQLite

Runtime state is per checkout, not global and not committed:

```text
proxydb/
  proxytools.db
  proxytools.db-wal
  proxytools.db-shm
  proxytools.lock
geodb/
  geoip.mmdb
  version
```

One process is allowed per checkout. Separate clones may run simultaneously
with separate databases. The current source workflow uses a kernel advisory
lock, not lock-file existence; stale lock metadata after a crash is harmless.

SQLite persists useful `STABLE` and `PROBATION` restart state only. Do not turn
it into a graveyard of dead candidates. Preserve schema migrations and never
delete a user's database merely to simplify a migration. `accepted` and
`reachable` remain distinct columns.

At monitor startup:

1. restore saved rows immediately;
2. recheck saved `STABLE`/`PROBATION` candidates through the active lane;
3. fetch and check new ProxyScrape candidates through discovery.

`./proxytools --clear` removes known generated runtime artifacts, local locks,
caches, bytecode, and `.venv` while preserving source, Git metadata, arbitrary
user exports, and `.env` files. Any new generated artifact requires coordinated
updates to `.gitignore`, cleanup code/tests, and user documentation.

## Documentation rules

- `README.md` at the repository root is the project's only `README.md` and the
  only user-facing source document.
- `DEVELOPERS.md` is the single human-facing home for internals, source
  development, testing, and contributor guidance.
- `BUILD.md` is the single human-facing runbook for all standalone-executable
  builds and their tests, whether local or PVE-assisted, including build-lab
  provisioning, release status, and operating procedures.
- `AGENTS.md` is the single home for AI-agent handoff context and durable chat
  agreements that should survive between sessions.
- Do not create nested `README.md` files, additional `BUILD*.md` files,
  separate runbooks, or other scattered Markdown documentation. Put
  implementation-specific details in module docstrings or comments beside the
  relevant code; otherwise update one of the four root documents above
  according to its audience.
- `release/pve/linux/` contains all Linux PVE provisioning, build orchestration,
  and infrastructure tests.
- `release/pve/windows/` is the exclusive home for future Windows PVE template,
  build, and infrastructure-test work.
- Module docstrings at the top of scripts should explain what the file is, why
  it exists, and how it is used.
- CLI `--help` output is derived from the corresponding module documentation
  where the command design requires it.

Update the appropriate documentation when behavior changes. Do not bury
end-user instructions in the developer guide or implementation internals in
the README.

## Release/build lab

Release/build work is active and split into local and PVE Linux workflows,
documented together in `BUILD.md`. The local workflow is the normal
contributor path. PVE access is never required for ordinary development, fixes,
tests, reviews, or pull requests. The remote workflow is an additional
maintainer release layer with implemented Debian build and Ubuntu compatibility
orchestration.

Generated local build output lives under ignored `release/.work/`. Local dirty-
worktree artifacts are for development only. A real release requires one clean,
checksummed source snapshot. Linux dependencies are locked in
`release/linux/constraints.txt`; update that file only through an explicit,
reviewed dependency refresh followed by a clean build and both smoke layers.

`release/linux/build.sh` always runs deterministic offline frozen smoke tests.
`release/linux/smoke-live.sh` is a separate bounded network check and must not
be folded into the ordinary contributor build gate.

Preserve `.work` after builds for diagnostics. `release/linux/build.sh` removes
the previous ignored `release/bin/` at startup and promotes the complete latest
artifact set there only after a successful build and offline smoke. Do not
commit either directory or treat a dirty-worktree artifact as publishable.
Live-smoke output belongs in `.work/local-linux/logs/` and must survive a
failed check. The distributed Linux set includes the executable, end-user
README, MIT `LICENSE`, manifest, and checksums. Each frozen build embeds its
exact UTC build time and source commit for `--about`.

The PVE server is `root@192.168.66.2`; Linux template `9000` is stopped,
protected, and immutable between explicit maintenance sessions. Never build in
the template or delete it. Build only in a disposable linked clone and apply
the validation and cleanup guards in `BUILD.md` and the orchestrator.
Ubuntu 24.04 LTS compatibility template `9001` is also stopped, protected, and
immutable. It is for running the returned standalone artifact in a disposable
linked clone, not for producing a second Linux build. Never boot or delete
either base template directly.
`release/pve/linux/provision-host.sh` is the authoritative clean-host bootstrap for
these templates. It validates rather than rewrites site-specific PVE storage
and networking, verifies official cloud-image checksums, never replaces an
occupied VMID/image automatically, and retains failed provisioning state for
diagnosis. Keep its invariants synchronized with `BUILD.md` and the actual
template contract.
Verified source media under `/var/lib/vz/template/iso/` is persistent build-lab
state. Keep cloud images, installation ISOs, and driver ISOs after successful
template creation so future maintenance does not download them again.
Automated provisioning and cleanup may remove incomplete `.part` files and
generated unattended-answer media only; it must never delete verified source
media. Validate cached media against its recorded upstream checksum before
reuse.
Before destroying any VM that has installation media attached, detach every
cached ISO/cloud image from its VM configuration and verify that the config no
longer references source media. PVE `qm destroy --purge 1` can delete attached
ISO volumes; exact VMID/name guards alone do not protect the media cache. Every
project script that invokes `qm destroy --purge 1` must enforce this
detach-and-recheck gate immediately before destruction and must fail closed on
an unsupported media reference.
`release/pve/linux/build.sh` is the implemented maintainer build/test orchestrator.
It builds the current worktree in a disposable Debian clone, validates the same
artifact through offline and live smoke in Ubuntu, retrieves artifacts/logs,
deletes only successful exact clones, and retains the active clone on failure.
Its generated state belongs under `release/.work/pve-linux/`; the artifact is
promoted to `release/bin/` only after both OS gates pass.
Its normal mode transfers the current worktree for development. Its explicit
`--release` mode requires a completely clean worktree, creates one checksummed
`git archive` from `HEAD`, and verifies that same archive in both guests.
Build and host provisioning share a PVE-side kernel lock; a contender must exit
before cleaning local state or touching VMs.
PVE orchestration regressions are infrastructure checks, not part of the
project's normal Python test suite. Keep them in the platform's dedicated
`release/pve/PLATFORM/tests/` directory and run Linux checks explicitly with
`./release/pve/linux/tests/test_build.sh` and
`./release/pve/linux/tests/test_provision_host.sh` when changing PVE build,
cleanup, or host-validation behavior. The root `tests/` directory contains only
Python tests for source application behavior and must not collect release-lab
or PVE environment checks.
Before a new run, it must reconcile clones retained by previous failures: only
exact unprotected non-template names `proxytools-debian-build-VMID` and
`proxytools-ubuntu-validation-VMID` may be shut down and deleted automatically.
Never weaken these guards or allow stale clones to accumulate across reruns.
The dedicated PVE build server `root@192.168.66.2` exists specifically so
agents can perform project build-lab work autonomously. Once the user requests
a PVE, template, build, test, validation, repair, or release-lab task, that
request is standing authorization to do everything reasonably required on this
one server to finish the task. Do not ask for confirmation for intermediate PVE
actions and do not make the user supervise the VM lifecycle. This authorization
applies only to `root@192.168.66.2`; it does not extend to any other PVE host,
server, workstation, external service, or infrastructure.

Proceed without further permission for SSH/SCP/rsync; uploading the required
public SSH keys, scripts, source, and artifacts; installing packages; changing
build-lab host, storage, service, or guest configuration; choosing VMIDs;
creating and modifying template candidates; creating, cloning, configuring,
starting, stopping, rebooting, and deleting temporary VMs; running guest
commands; retrieving logs; and reconciling leftovers from failed attempts. This
authorization is durable across chats and applies equally to Linux and Windows
build-lab work. Resolve routine operational choices yourself.

There are two non-negotiable preservation rules on this PVE build server:

1. Never delete ISO files or other verified source installation media. Detach
   them safely from a VM before purge and leave the cached files on the host.
2. Never delete a verified template. A template candidate may be freely built,
   repaired, replaced, or discarded before verification; after it has passed
   its template validation and become a retained build-lab template, preserve
   it. Ordinary work uses disposable clones rather than booting the verified
   template itself.

These preservation rules are hard stops, not prompts for more permission. Keep
exact identity checks, bounded waits, protected-template validation, and the
cleanup guards in `BUILD.md` and the orchestration code. If a destructive
target cannot be proven to be a disposable VM or unverified template candidate,
fail closed and diagnose it rather than asking to delete a protected asset.

Do not present a tool or sandbox approval dialog as a new decision about
whether the PVE action is allowed. If the execution environment mechanically
requires approval, describe it only as technical access for the already
authorized project operation and continue when access is granted.

Remote Linux orchestration is implemented and proven under `release/pve/linux/`.
Windows PVE work belongs only under `release/pve/windows/`: use the Linux
provisioner as the lifecycle/safety reference, keep guest-specific mechanics
narrow, and do not add or run Windows project packaging until the template is
repeatable and the user accepts it.

The supported standalone scope is:

- native Linux x86_64 executable targeting current stable/LTS distributions;
- native Windows x86_64 executable built on current Windows 11 Enterprise
  Evaluation, with Windows 10 compatibility expected but unvalidated until a
  native Windows 10 smoke run;
- Selenium embedded unconditionally; browser external;
- `README.md` beside each distributed binary;
- external config and runtime data beside the binary;
- clear error if that directory is not writable;
- one PVE VM-based workflow for both operating systems, no container/VM
  mixture;
- ephemeral Linux and Windows linked clones controlled from the development
  machine through the PVE API/SSH and guest SSH/SCP/rsync;
- cross-platform process locking with `portalocker` before Windows packaging;
- embedded defaults that create external `proxytools.conf` on first run.

Release work happens only for stable versions in a temporary release branch
and separate worktree. Both binaries must eventually be built from the same
clean commit and pass native automated tests. Manual TUI acceptance happens
before the user declares a release ready and is not part of the release
pipeline. See `BUILD.md` for the human-facing workflow and current status.

## Git collaboration policy

- Never commit or push unless the user explicitly authorizes a commit.
- Authorization to commit also authorizes an immediate normal push.
- Before committing, run all relevant validation and inspect the exact diff.
- Commits must be small, coherent, and safe to roll back independently.
- Fix mistakes with an additional normal commit when appropriate.
- Never rebase, amend, force-push, or create revert commits as routine repair.
- Preserve unrelated modified/untracked files in a dirty worktree.
- Do not add generated databases, VM images, ISO files, credentials, keys,
  `.venv`, build artifacts, or local logs to Git.

## Required validation

For normal Python changes, run from the repository root:

```bash
./.venv/bin/python -m unittest discover -v
find src/proxytools -name '*.py' -print0 \
  | xargs -0 ./.venv/bin/python -m py_compile
sh -n proxytools
git diff --check
git status --short --branch
```

Tests must remain offline and deterministic: mock ProxyScrape, DB-IP, identity
services, target sites, browsers, clocks, and filesystem homes as appropriate.
For a bug fix, add a regression test that fails under the old behavior and
captures the intended invariant rather than merely increasing coverage.

## Communication style and decision discipline

The user communicates primarily in Ukrainian and prefers direct, practical
technical discussion. Be candid about trade-offs and point out overengineering
or unsafe assumptions. For requested implementation, proceed autonomously
inside the agreed scope. When the user explicitly says to discuss a feature or
not edit yet, do not modify files until agreement is reached.

Treat durable project decisions made in chat as repository knowledge, not
session-only memory. When a new agreement will govern future implementation,
operations, safety, release work, or user experience, update `AGENTS.md` in the
same change without waiting for a separate reminder. Keep detailed human-facing
development procedures in `DEVELOPERS.md`, standalone build procedures in
`BUILD.md`, or implementation-specific details beside the relevant code, and
record the concise invariant here. Do not add temporary observations, one-off
task status, credentials, host secrets, or speculative plans to `AGENTS.md`.

Lead handoff messages with the result. Report changed behavior, tests actually
run, remaining risks, and whether a commit/push was performed. Do not claim a
release, build, test, commit, or push that did not actually happen.
