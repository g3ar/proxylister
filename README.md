# Proxy Tools

`proxytools` is a single CLI application for working with free proxies from [ProxyScrape](https://proxyscrape.com/free-proxy-list). It provides two modes:

- **`list`** — one-shot fetch, check, geolocation, and stdout output; this is the default.
- **`monitor`** — continuous stability monitoring in a live terminal dashboard.

## Requirements

- Python 3.10+
- `requests[socks]` (includes PySocks for `socks4`/`socks5` proxies)
- `selenium>=4.10` is installed with the base dependencies but used only by `list --browser-check`. Google Chrome is required for that mode; Selenium Manager downloads a matching ChromeDriver automatically.
- Rich provides progress and status output for non-interactive commands.
- Textual provides the interactive `monitor` dashboard.

## Installation

Clone the repository and use the single root launcher:

```bash
git clone <repository-url>
cd proxylister
./proxytools --help
```

On the first real command, `./proxytools` creates an ignored `.venv` and installs the project and all dependencies declared in `pyproject.toml`. That file is the single dependency manifest. Selenium is imported and invoked only when `list --browser-check` is requested. Python 3 with the standard `venv` module must be available on the host.

`./proxytools` is the only supported user-facing entrypoint:

```bash
./proxytools list --help
./proxytools monitor --help
```

To return a clone to its just-cloned runtime state, run `./proxytools --clear`. It refuses to run while another Proxy Tools command owns the clone lock, then removes the local virtual environment, SQLite and GeoIP databases, legacy `working_proxies.txt` output, lock file, Python bytecode, test caches, and build artifacts. Source files, Git metadata, `.env` files, and redirected user output are preserved. Removed local state is not recoverable.

## Configuration

Technical defaults live in the tracked `proxytools.conf` beside the launcher. It is a strict, flat `KEY=value` file with detailed `#` comments for every setting. Unknown keys, duplicates, missing keys, invalid types, and invalid ranges stop startup with a configuration error. The file is parsed as data and is never sourced or executed as shell code. CLI values override `URL` and `MAX_LATENCY` for one invocation.

## `list`

Fetches proxies for all protocols (`http`, `socks4`, `socks5`), dedupes them, checks which are alive concurrently, geolocates each, and prints those under `--max-latency` to stdout, sorted fastest first. Omitting the mode is equivalent to `list`.

```bash
./proxytools --max-latency 500 > working.txt
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--max-latency` | Only keep proxies faster than this (ms) | `MAX_LATENCY` |
| `--url` | URL every proxy must reach through a lightweight request | `URL` |
| `--debug` | Print detailed metadata instead of connection strings | off |
| `--browser-check` | Additionally validate `--url` with Selenium | off |
| `--headless` | Hide the Selenium browser used by `--browser-check` | off |

Press **Ctrl+C** any time to stop early. Confirmed results collected so far are still printed.

### Output

Normal stdout contains one directly usable connection string per line:

```text
http://203.0.113.10:8080
socks5://198.51.100.20:1080
```

Progress and diagnostics go to stderr, so redirection and pipelines remain clean. `--debug` changes stdout rows to the detailed latency, protocol, country, coordinates, and maps representation.

### `--url` and `--browser-check`

`--url` applies the same lightweight requests-based target check used by monitor. Add `--browser-check` for a second Selenium validation in one Chrome instance at a time. A proxy is dropped if the page fails to load, Chrome shows an internal network-error page, or the main document has an HTTP error status. On success a visible window stays open for 10 seconds so the result can be inspected; `--headless` hides it and skips that delay. The page-load timeout is `2 × --max-latency`, floored at 10s. Both `--browser-check` and `--headless` require the preceding options they operate on.

## `monitor`

Runs forever and keeps a rolling check history for each proxy. New candidates start in `PROBATION`, become `STABLE` only after satisfying every configured time and quality threshold, and change to `DEGRADED` after a failure. Green, yellow, and red rows represent those states.

Monitor state is stored in `proxytools.db` beside the root launcher. Each clone therefore has an independent database. Only working `STABLE` and `PROBATION` proxies are retained between runs; a failed or `DEGRADED` proxy and its detailed history are removed. Recent checks restore the rolling history after a restart, but a pause longer than twice `MONITOR_REFRESH_INTERVAL` is not counted as continuous uptime. Details for retained proxies are pruned after 24 hours while their aggregates and transitions remain. SQLite WAL companion files are expected while the monitor is running. All database and lock files are ignored by Git.

At startup, saved proxies and their last known statuses are shown immediately. A `*` on a status means it came from the database and is awaiting verification. Their active checks start at once while ProxyScrape is fetched independently; overlap protection prevents the same proxy from running in both lanes simultaneously.

During long-running monitoring, checks use two independent lanes. Roughly 20% of `WORKERS` are reserved for active `STABLE`/`PROBATION` proxies and run every `MONITOR_REFRESH_INTERVAL`; the remaining workers discover proxies from ProxyScrape in parallel. A successful discovery candidate joins the active lane immediately, so probation progress is not blocked by a large discovery backlog. The status bar reports both lane counters. With `WORKERS=1`, each lane receives one worker so neither can starve the other.

```bash
./proxytools monitor --max-latency 500
```

| Flag | Description | Default |
|------|-------------|---------|
| `--max-latency` | Only admit proxies faster than this (ms) | `MAX_LATENCY` |
| `--debug` | Show City, Exit IP, and Blocked by diagnostics | off |
| `--url` | HTTP(S) URL every proxy must reach; also opened by `b` | `URL` |

**Controls:** arrow keys select and scroll rows, `q` quits, `s` opens a multi-select state picker, `p` opens a multi-select protocol picker, `c` opens a searchable country picker, and `b` opens the selected proxy in a disposable private browser session. By default the table shows stable and probationary proxies while degraded proxies are hidden. State, protocol, and exact-country filters combine; choose `All countries` to clear the country filter. Rows update in place while checks run and are reordered only after an active-pool pass completes. Textual's footer always shows the active bindings.

The browser action uses `MONITOR_BROWSER` from the config. In `auto` mode it detects Chrome/Chromium first and Firefox second. Chrome runs with incognito mode and a temporary user-data directory; Firefox runs in private mode with a generated temporary profile. Both receive the selected HTTP, SOCKS4, or SOCKS5 proxy without reading or changing the normal browser profile. Only one browser session can be launched by a monitor at a time. The browser may outlive the monitor; its detached lifecycle helper removes the temporary profile after the browser closes.

When `URL` is configured or `--url` is supplied, every otherwise successful proxy check makes one additional lightweight `requests` request to that URL through the same proxy. HTTP 2xx/3xx passes. Monitor also accepts HTTP 403 because anti-bot sites may reject `requests` while remaining usable in the interactive browser; other 4xx/5xx responses, timeout, TLS, proxy, and redirect errors fail with the `url` blocker. This happens once after the configured proxy samples and never invokes Selenium. Without a URL there is no target request and `b` opens `about:blank`.

The Textual table is scrollable, supports row selection, and updates a detail panel for the highlighted proxy. Its default view shows state, country, continuous live time, total observed uptime, first seen and last failure times, check count, success streak, rolling success rate, median latency, p95 latency, jitter, and connection string. `--debug` additionally shows City, Exit IP, and `Blocked by`. Each base sample makes one HTTPS request to the neutral `api.ipify.org` identity endpoint through the proxy. The returned exit IP is therefore measured on the same HTTPS route a browser uses, while country, city, and coordinates are resolved locally rather than accepted from the identity service. `Blocked by` explains why a row is not yet stable: `alive`, `checks`, `rate`, `streak`, `latency`, `jitter`, `url`, or `failed`. A status bar reports the current phase, cycle progress, tracked/stable counts, and active filters. A proxy that disappears from ProxyScrape continues to be checked until `MONITOR_RETENTION_TIME` expires.

On each real `list` or `monitor` start, Proxy Tools checks the monthly DB-IP City Lite database stored as `proxytools-geoip.mmdb` beside the launcher. A missing or outdated database is downloaded over HTTPS, validated, decompressed, and atomically replaced; both the database and its version marker are ignored by Git. If an update fails, the existing database remains usable. With no local database, checks continue and locations are reported as `Unknown`. GeoIP data attribution: [IP Geolocation by DB-IP](https://db-ip.com).

Only one working command (`list` or `monitor`) may run from a given clone at a time. The kernel-backed `proxytools.lock` is released automatically even after a crash. Separate clones use separate locks and databases and can run simultaneously. Help and version commands never acquire the lock.

A new proxy must respond below `MAX_LATENCY` (or its CLI override) to qualify as `STABLE`. Once admitted, a slow response remains a quality miss in its rolling history but does not mean the proxy is dead or demote it by itself. By default, `STABLE` survives the number of hard failures configured by `MONITOR_ALIVE_FAILURE_TOLERANCE`; the next failure moves it to `PROBATION`, resets continuous live time, and starts the `MONITOR_DEGRADED_AFTER` grace period. Continued hard failures after that period produce `DEGRADED`, while one clean check restores a previously stable proxy immediately. Recovery metadata and the grace timer are stored in SQLite and survive restarts.

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
proxytools.conf            commented host/runtime defaults
pyproject.toml             package metadata and installed console script
src/proxytools/
  cli.py                   top-level command dispatcher
  commands/                list and monitor orchestration
  sources/                 ProxyScrape API adapter
  checking/                HTTP and optional browser validation
  monitoring.py            UI-independent engine and immutable snapshots
  browser.py               disposable interactive browser launcher
  browser_session.py       detached temporary-profile lifecycle helper
  stability/               rolling history and stability policy
  storage/                 versioned SQLite schema, restoration, and persistence
  output/                  list formatting and split Textual dashboard widgets
  process_lock.py          per-clone kernel process lock
  paths.py                 clone-local runtime path resolution
  models.py                shared domain records
  config.py                strict config loading and CLI value validation
tests/                     isolated unit tests with mocked network access
```

## Notes

- Free proxies are short-lived and unreliable — expect a low success rate.
- Tune `WORKERS` for faster listing (more open connections), or `TIMEOUT` on a slow connection.
- `--browser-check` is slow by nature and deliberately uses one Chrome instance at a time.
- On a headless server, pair `--browser-check` with `--headless`, or Chrome will fail to launch.

## License

MIT License.
