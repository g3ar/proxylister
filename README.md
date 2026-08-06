# Proxy Tools

Proxy Tools finds public HTTP, SOCKS4, and SOCKS5 proxies, verifies them through a real HTTPS connection, measures latency, identifies the exit IP and country, and can continue monitoring promising proxies until they prove stable.

The project has one entrypoint and two modes:

- `list` performs a single scan and prints usable proxy addresses;
- `monitor` continuously checks proxies in an interactive terminal interface.

The default mode is `list`, so `./proxytools` works immediately after cloning.

## Quick start

Requirements:

- Linux;
- Python 3.10 or newer with the `venv` module;
- internet access;
- Chrome or Chromium only when Selenium validation is requested;
- Chrome/Chromium or Firefox for the monitor's interactive browser action.

Clone the project and run it:

```bash
git clone <repository-url>
cd proxylister
./proxytools
```

On the first real run, the launcher creates a local `.venv` and installs the required Python packages. Nothing needs to be installed manually with `pip`.

Useful help commands:

```bash
./proxytools --help
./proxytools list --help
./proxytools monitor --help
./proxytools --version
```

## Typical use cases

### Get a plain list of working proxies

```bash
./proxytools
```

Normal output contains one connection string per line, sorted by latency:

```text
http://203.0.113.10:8080
socks5://198.51.100.20:1080
```

Progress is written to stderr, while proxy addresses are written to stdout. This makes redirection and pipelines safe:

```bash
./proxytools > working-proxies.txt
./proxytools | head -n 10
```

### Find proxies suitable for a particular website

```bash
./proxytools --url https://example.com
```

For each proxy, Proxy Tools first performs its normal HTTPS identity check and then requests the supplied URL through the same proxy. A proxy is printed only when the complete check succeeds.

This is useful when a proxy works generally but the destination website blocks it, returns an error, or cannot be reached from that exit location.

### Limit acceptable latency

```bash
./proxytools --max-latency 300
```

Only proxies with a median latency below 300 ms are printed. The default comes from `MAX_LATENCY` in `proxytools.conf`.

### Inspect detailed scan results

```bash
./proxytools --debug
```

Debug output includes latency, protocol, address, connection string, country, coordinates, and a map link. It is intended for inspection rather than direct use as a proxy list.

### Validate a website in a real browser

```bash
./proxytools list \
  --url https://example.com \
  --browser-check
```

After lightweight checks pass, Selenium opens Chrome through each candidate proxy and verifies that the page loads. Browser checks run one at a time because launching many Chrome instances is expensive.

By default, a successful browser remains visible briefly for inspection. On a server without a graphical session, use:

```bash
./proxytools list \
  --url https://example.com \
  --browser-check \
  --headless
```

`--browser-check` requires `--url`, and `--headless` requires `--browser-check`.

### Search for stable proxies over time

```bash
./proxytools monitor
```

The monitor immediately restores previously saved candidates, rechecks them, downloads fresh candidates from ProxyScrape, and keeps both groups moving through independent work queues.

The compact table shows `State`, `Country`, `Median`, `Alive`, and `Connection` only for proxies whose latency has actually been measured. Rows are grouped by status and sorted by latency inside each group:

1. `STABLE`;
2. `PROBATION`;
3. `DEGRADED`.

### Monitor access to one specific website

```bash
./proxytools monitor --url https://whatismyipaddress.com/
```

Every proxy must pass both the normal identity check and a lightweight request to this URL. HTTP 403 is accepted because anti-bot sites frequently reject `requests` while remaining usable in an interactive browser. Other HTTP errors and network failures fail the complete check.

When a URL is configured, pressing `b` opens that same address through the selected proxy.

### Monitor with diagnostic columns

```bash
./proxytools monitor --debug
```

This restores the full diagnostic table: checks, streak, success rate, P95, jitter, city, measured exit IP, and the conditions currently preventing a proxy from becoming stable. It also shows separate active-pool and discovery-pool counters in the status bar.

## Monitor controls

| Key | Action |
|-----|--------|
| Arrow keys | Select and scroll rows |
| `s` | Choose visible statuses |
| `p` | Choose visible protocols |
| `c` | Search for and select a country |
| `b` | Open the selected proxy in a private browser session |
| `y` | Copy the selected connection string to the terminal clipboard |
| `q` | Stop the monitor and quit |

State, protocol, and country filters work together. By default, `STABLE` and `PROBATION` are visible and `DEGRADED` is hidden.

Rows remain in place while an active checking pass is running and are reordered after the pass completes. This prevents the selected proxy from constantly jumping around the table.

Copying uses the terminal's OSC 52 clipboard protocol and requires no `xclip` or `xsel`. Most current terminals support it locally and through SSH, although a terminal or multiplexer may disable clipboard escape sequences for security.

When `q` is pressed, the monitor displays a stopping message and waits for active requests to finish or reach their configured timeout.

## Proxy statuses

### `PROBATION`

The proxy is still proving itself, recovering from failures, or has not yet met every configured stability condition. Typical blockers include insufficient live time, too few checks, an insufficient success rate, high latency, excessive jitter, or failure to reach the configured URL.

### `STABLE`

The proxy has remained available long enough and satisfies the required history, streak, success-rate, latency, and jitter thresholds.

A small number of isolated failures is tolerated. A previously stable proxy can recover after one clean complete check instead of repeating the entire initial probation period.

### `DEGRADED`

The proxy continued to fail after its recovery grace period. Degraded proxies are hidden by default but can be enabled with the `s` status filter.

### Restored marker `*`

A status ending in `*` was restored from the local database and has not yet been verified during the current run. The asterisk is only a marker; it is not a separate status.

## Understanding the measurements

- **Alive** — continuous healthy period used for stability admission; tolerated isolated failures do not reset it immediately.
- **Checks** — rolling checks retained versus the number required for admission.
- **Streak** — consecutive complete successful checks.
- **OK** — complete success rate over the rolling history.
- **Median** — median of measured proxy latencies.
- **P95** — 95th-percentile measured latency.
- **Jitter** — deviation of measured latencies.
- **Country** — location resolved locally from the measured HTTPS exit IP.

Proxy Tools distinguishes two important facts internally:

- **reachable** means the proxy answered and latency was measured;
- **accepted** means the complete check passed, including latency and the optional target URL.

This is why a proxy can have a measured median while still being in probation or degraded.

## Opening a selected proxy in a browser

Press `b` in the monitor to open the selected proxy in a disposable private session.

- Chrome/Chromium uses incognito mode and a temporary user-data directory.
- Firefox uses private mode and a generated temporary profile.
- The regular browser profile, cookies, history, and proxy settings are not modified.
- Only one browser session can be launched by one monitor at a time.
- Temporary profile data is removed after the launched browser closes.

`MONITOR_BROWSER` in `proxytools.conf` selects `auto`, `chrome`, or `firefox`. In `auto` mode, Chrome/Chromium is preferred and Firefox is used as a fallback.

Private browsing isolates the temporary session from the main profile. It should not be treated as a complete anonymity guarantee.

## Configuration

Persistent defaults live in `proxytools.conf` beside the launcher:

```text
KEY=value
```

Lines beginning with `#` and blank lines are ignored. Inline comments are supported. The configuration is parsed as plain data and is never executed as a shell script.

The parser is intentionally strict. Unknown keys, duplicate keys, missing required keys, invalid values, and invalid ranges stop startup with a useful error.

### General settings

| Key | Purpose |
|-----|---------|
| `TIMEOUT` | Maximum seconds for one network request |
| `WORKERS` | Concurrent network checks, from 1 to 100 |
| `SAMPLES` | Requests used for one latency measurement, from 1 to 5 |
| `MAX_LATENCY` | Maximum acceptable median latency in milliseconds |
| `URL` | Optional website every proxy must reach; empty disables the check |

### Monitor settings

| Key | Purpose |
|-----|---------|
| `MONITOR_REFRESH_INTERVAL` | Delay between active-pool passes |
| `MONITOR_HISTORY_SIZE` | Recent measurements retained per proxy |
| `MONITOR_MIN_CHECKS` | Checks required for initial stable admission |
| `MONITOR_MIN_SUCCESS_RATE` | Required complete success fraction from 0 to 1 |
| `MONITOR_MIN_SUCCESS_STREAK` | Consecutive successes required for admission |
| `MONITOR_MIN_ALIVE_TIME` | Continuous accepted uptime required for admission |
| `MONITOR_MAX_JITTER` | Maximum permitted latency deviation in milliseconds |
| `MONITOR_ALIVE_FAILURE_TOLERANCE` | Hard failures tolerated while retaining `STABLE` |
| `MONITOR_DEGRADED_AFTER` | Failed seconds before probation becomes degraded |
| `MONITOR_RETENTION_TIME` | Seconds to track proxies no longer advertised by ProxyScrape |
| `MONITOR_BROWSER` | Browser used by `b`: `auto`, `chrome`, or `firefox` |

The comments inside `proxytools.conf` describe valid ranges and defaults in more detail.

CLI options override `URL` and `MAX_LATENCY` for one invocation without modifying the file:

```bash
./proxytools monitor \
  --url https://example.com \
  --max-latency 350
```

## Local data

Generated state is stored beside the launcher but outside the project root's visible file list:

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

`proxydb/` contains monitor history and the per-clone process lock. `geodb/` contains the automatically downloaded monthly DB-IP City Lite database. Both directories are ignored by Git.

Existing files from older versions are moved from the project root into these directories automatically.

Each clone has its own state. Two separate clones may run simultaneously, but two working Proxy Tools commands cannot run from the same clone at the same time.

## Resetting local state

```bash
./proxytools --clear
```

This returns the directory to its freshly cloned runtime state by removing:

- `.venv` and installed dependencies;
- proxy history and locks in `proxydb/`;
- the downloaded GeoIP database in `geodb/`;
- Python bytecode and common test/build caches;
- legacy generated files from older versions.

Source files, Git metadata, `.env` files, and arbitrary redirected user output are preserved. The deleted runtime state cannot be recovered. Cleanup refuses to run while another command owns the clone lock.

## Troubleshooting

### The first run takes a while

The first real command creates `.venv`, installs dependencies, and downloads the monthly GeoIP database. Later starts reuse them.

### Few or no proxies are found

Public proxies are short-lived and unreliable. Try a larger `TIMEOUT`, a higher `MAX_LATENCY`, or another run after ProxyScrape updates its lists. A restrictive `--url` can reduce the result set dramatically.

### Browser validation fails

Ensure Chrome or Chromium is installed. Use `--headless` when no graphical desktop is available. Selenium Manager may need internet access to obtain a compatible driver.

### The `b` action cannot find a browser

Install Chrome, Chromium, or Firefox, or set `MONITOR_BROWSER` to a browser that is present on the host.

### Country differs from a website's result

Proxy Tools resolves the measured HTTPS exit IP through a local monthly GeoIP database. Commercial websites may use newer or different location data, and some proxies route different destinations through different exits. Country data is therefore useful for filtering but cannot be guaranteed to match every website.

### A proxy has latency but is not stable

Latency only proves reachability. Stable admission also depends on elapsed live time, check count, success rate, streak, jitter, and the optional target URL.

## Further technical information

Architecture, data flow, status algorithms, SQLite persistence, bootstrap behavior, testing, and contribution guidance are documented in [DEVELOPERS.md](DEVELOPERS.md).

## License

MIT License.
