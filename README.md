# Proxy Tools

`proxytools` is a single CLI application for working with free proxies from [ProxyScrape](https://proxyscrape.com/free-proxy-list). It provides two subcommands:

- **`scan`** — one-shot fetch, check, geolocation, export, and optional browser validation.
- **`monitor`** — continuous stability monitoring in a live terminal dashboard.

## Requirements

- Python 3.10+
- `requests[socks]` (includes PySocks for `socks4`/`socks5` proxies)
- `selenium>=4.10` is installed with the base dependencies but used only by `scan --check-url`. Google Chrome is required for that mode; Selenium Manager downloads a matching ChromeDriver automatically.
- Rich provides progress and status output for non-interactive commands.
- Textual provides the interactive `monitor` dashboard.

## Installation

Clone the repository and use the single root launcher:

```bash
git clone <repository-url>
cd proxylister
./proxytools --help
```

On the first real command, `./proxytools` creates an ignored `.venv` and installs all Python dependencies automatically. Selenium is imported and invoked only when `scan --check-url` is requested. Python 3 with the standard `venv` module must be available on the host.

`./proxytools` is the only supported user-facing entrypoint:

```bash
./proxytools scan --help
./proxytools monitor --help
```

## `scan`

Fetches proxies for all protocols (`http`, `socks4`, `socks5`), dedupes them, checks which are alive concurrently, geolocates each, and writes the ones under `--max-latency` to a file, sorted fastest first.

```bash
./proxytools scan --timeout 5 --workers 50 --output working.txt --max-latency 500
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--timeout` | Seconds to wait per proxy check | `5` |
| `--workers` | Concurrent worker threads | `50` |
| `--output` | Output file path | `working_proxies.txt` |
| `--format` | Output format: `text`, JSON Lines (`json`), or `csv` | `text` |
| `--max-latency` | Only keep proxies faster than this (ms) | `500` |
| `--samples` | Checks per proxy; use their median duration (1–5) | `1` |
| `--check-url` | URL to validate each fast proxy against via Selenium (see below) | disabled |
| `--browser-workers` | Maximum concurrent Chrome instances | `1` |
| `--headless` | Run `--check-url` checks without a visible browser window | off |

Press **Ctrl+C** any time to stop early — only proxies already confirmed working (and Selenium-verified, if applicable) at that point get saved.

### Output format

One proxy per line:

```
<latency>ms <protocol> <ip:port> <connection string> <country> <lat,lon> <google maps link>
```

Example:

```
842ms socks5 62.133.62.207:1081 socks5://62.133.62.207:1081 Germany 51.2993,9.491 https://www.google.com/maps?q=51.2993,9.491
```

### `--check-url` (Selenium validation)

Fast proxies first receive a lightweight HTTP check against the target URL. Those that pass are sent to a separate browser-worker pool, so Chrome does not block collection of network-check results. A proxy is dropped if the page fails to load, Chrome shows an internal network-error page, or the main document has an HTTP error status. On success a visible window stays open for 10 seconds; headless mode skips that delay. The page-load timeout is `2 × --max-latency`, floored at 10s.

## `monitor`

Runs forever and keeps a rolling check history for each proxy. New candidates start in `PROBATION`, become `STABLE` only after satisfying every configured time and quality threshold, and change to `DEGRADED` after a failure. Green, yellow, and red rows represent those states.

Monitor state is stored in `proxytools.db` beside the root launcher. Each clone therefore has an independent database. Recent checks restore the rolling history after a restart, but a pause longer than twice `--refresh-interval` is not counted as continuous uptime. Detailed checks are retained for 24 hours; lifetime counters and state transitions remain in the database. SQLite WAL companion files are expected while the monitor is running. All database and lock files are ignored by Git.

At startup, saved proxies and their last known statuses are loaded before any ProxyScrape request. A `*` on a status means it came from the database and is awaiting verification. Saved proxies are checked first; then ProxyScrape is fetched and only newly discovered addresses are checked, so an overlapping proxy is never checked twice in the startup cycle. The normal refresh cycles begin afterward.

```bash
./proxytools monitor --timeout 5 --workers 50 --max-latency 500 --min-alive-time 60
```

| Flag | Description | Default |
|------|-------------|---------|
| `--timeout` | Seconds to wait per proxy check | `5` |
| `--workers` | Concurrent worker threads | `50` |
| `--max-latency` | Only track proxies faster than this (ms) | `500` |
| `--samples` | Checks per proxy; use their median duration (1–5) | `1` |
| `--refresh-interval` | Delay between complete scan cycles (seconds) | `10` |
| `--history-size` | Recent check results retained per proxy | `10` |
| `--min-checks` | Checks required before a proxy can become stable | `5` |
| `--min-success-rate` | Required success ratio from `0` to `1` | `0.8` |
| `--min-success-streak` | Consecutive successful checks required | `3` |
| `--min-alive-time` | Required continuous live time in seconds | `60` |
| `--max-jitter` | Maximum latency standard deviation in ms | `150` |
| `--alive-failure-tolerance` | Failures allowed before continuous live time resets | `0` |
| `--retention-time` | Continue tracking proxies absent from ProxyScrape for this many seconds | `1800` |
| `--stable-only` | Hide probation and degraded proxies | off |
| `--browser` | Browser for the `b` action: `auto`, `chrome`, or `firefox` | `auto` |
| `--browser-url` | Initial page opened in a disposable browser session | `about:blank` |
| `--reset-history` | Delete this clone's saved history before starting | off |

**Controls:** arrow keys select and scroll rows, `q` quits, `p` pauses/resumes display updates (checks continue), `s` toggles stable-only mode, `c` opens a case-insensitive country filter, `r` requests the next scan immediately, and `b` opens the selected proxy in a disposable private browser session. Submit an empty country filter to show every country again. Textual's footer always shows the active bindings.

The browser action detects Chrome/Chromium first and Firefox second when `--browser auto` is used. Chrome runs with incognito mode and a temporary user-data directory; Firefox runs in private mode with a generated temporary profile. Both receive the selected HTTP, SOCKS4, or SOCKS5 proxy without reading or changing the normal browser profile. Only one browser session can be launched by a monitor at a time. The browser may outlive the monitor; its detached lifecycle helper removes the temporary profile after the browser closes. Selenium is not used for interactive browser sessions.

The Textual table is scrollable, supports row selection, and updates a detail panel for the highlighted proxy. It shows state, continuous live time, total observed uptime, first seen and last failure times, check count, success streak, rolling success rate, median latency, p95 latency, jitter, blocking criteria, and connection string. `Blocked by` explains why a row is not yet stable: `alive`, `checks`, `rate`, `streak`, `latency`, `jitter`, or `failed`. A status bar reports the current phase, cycle progress, tracked/stable counts, active filter, and countdown to the next cycle. A proxy that disappears from ProxyScrape continues to be checked until `--retention-time` expires.

Only one working command (`scan` or `monitor`) may run from a given clone at a time. The kernel-backed `proxytools.lock` is released automatically even after a crash. Separate clones use separate locks and databases and can run simultaneously. Help and version commands never acquire the lock.

A successful check requires a duration below `--max-latency`. By default, any failed check resets continuous live time. Setting `--alive-failure-tolerance 1`, for example, preserves the original live-time counter through one isolated failure, although the proxy still becomes `DEGRADED` immediately.

## Tests

The standard-library test suite mocks all network access:

```bash
PYTHONPATH=src python -m unittest discover -v
```

## Project structure

The application uses a `src` package layout. Command modules only orchestrate
the independent domain and adapter layers:

```text
proxytools                 root launcher and environment bootstrap
pyproject.toml             package metadata and installed console script
src/proxytools/
  cli.py                   top-level command dispatcher
  commands/                scan and monitor orchestration
  sources/                 ProxyScrape API adapter
  checking/                HTTP and optional browser validation
  monitoring.py            UI-independent engine and immutable snapshots
  browser.py               disposable interactive browser launcher
  browser_session.py       detached temporary-profile lifecycle helper
  stability/               rolling history and stability policy
  storage/                 SQLite schema, restoration, and batched persistence
  output/                  Rich console output, serializers, and Textual dashboard
  process_lock.py          per-clone kernel process lock
  paths.py                 clone-local runtime path resolution
  models.py                shared domain records
  config.py                shared CLI value validation
tests/                     isolated unit tests with mocked network access
```

## Notes

- Free proxies are short-lived and unreliable — expect a low success rate.
- `ip-api.com`'s free tier is rate-limited to 45 requests/minute per source IP, but since each lookup goes out through a different proxy, this rarely matters.
- Raise `--workers` for faster scans (more open connections), or `--timeout` on a slow connection.
- `--check-url` is slow by nature; raise `--browser-workers` carefully because each worker launches Chrome.
- On a headless server, pair `--check-url` with `--headless`, or Chrome will fail to launch.

## License

MIT License.
