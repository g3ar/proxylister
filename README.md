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
./proxytools --about
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

### Monitor proxies until they become stable

```bash
./proxytools monitor
```

The monitor continuously finds, checks, and rechecks proxies in a full-screen terminal interface. It is useful when a single scan is not enough and you want candidates that have remained healthy across multiple checks over time.

At startup, the monitor immediately shows useful candidates saved during earlier runs, verifies them again, and begins checking newly discovered proxies. Its state is saved automatically, so stopping and restarting the monitor does not discard previously established `STABLE` and `PROBATION` candidates.

The main screen contains a status line, a compact proxy table, and a footer with keyboard controls. The table shows only proxies for which a latency measurement is available:

| Column | Meaning |
|--------|---------|
| `State` | Current stability classification; `*` marks a restored proxy awaiting its first fresh check |
| `Country` | Country resolved from the exit IP observed through the proxy |
| `Median` | Rolling median response latency |
| `Alive` | Current continuous healthy period |
| `Connection` | Complete proxy URL ready to copy or use |

Rows are grouped by state and then ordered by latency:

1. `STABLE`;
2. `PROBATION`;
3. `DEGRADED`.

Rows remain in place while checks are completing and are reordered at controlled pass boundaries. This keeps the selected row from jumping while you navigate the table.

The status line reports the current cycle and activity, how many proxies are stable or tracked, how many rows are visible, and which filters are active. During shutdown it is replaced with a clear stopping message.

### Monitor access to one specific website

```bash
./proxytools monitor --url https://whatismyipaddress.com/
```

Every proxy must pass both the normal HTTPS identity check and a lightweight request to this URL. HTTP 403 is accepted because anti-bot sites frequently reject automated requests while remaining usable in a browser. Other HTTP errors and network failures prevent the check from being accepted.

When a URL is configured, pressing `b` opens that same address through the selected proxy.

## Using the monitor

| Key | Action |
|-----|--------|
| Arrow keys | Select and scroll rows |
| `F1` | Show project information, version, and contributor credits |
| `Enter` | Open detailed analytics for the selected proxy; `Enter` or `Esc` closes it |
| `s` | Choose visible statuses |
| `p` | Choose visible protocols |
| `c` | Search for and select a country |
| `b` | Open the selected proxy in a private browser session |
| `y` | Copy the selected connection string to the terminal clipboard |
| `q` | Stop the monitor and quit |

### Inspect one proxy

Select a row and press `Enter` to open its complete analytics without leaving the monitor. The detail window shows:

- connection string, state, and restored marker;
- country, city, and measured exit IP;
- healthy time, total observed uptime, retained checks, success streak, and success rate;
- current conditions preventing stable admission;
- median latency, P95 latency, and jitter;
- first observation and most recent failure times.

Press `Enter` or `Esc` to close the detail window and return to the same table selection.

### Filter the table

The state, protocol, and country filters work together. By default, the table includes `STABLE` and `PROBATION`, all supported protocols, and every country. `DEGRADED` proxies are hidden until enabled with `s`.

- Press `s` or `p`, use the arrow keys and Space to change selections, then press `Enter` to apply.
- Press `c`, type part of a country name, use the arrows if needed, and press `Enter` to apply.
- Choose `All countries` in the country picker to clear that filter.

The status line always lists the filters currently applied. An empty selection is allowed and produces an empty table until the filter is changed again.

### Copy or open the selected proxy

Press `y` to copy the complete connection string, such as `socks5://198.51.100.20:1080`. Copying uses the terminal's OSC 52 protocol and requires no `xclip` or `xsel`. Most current terminals support it locally and through SSH, although terminal or multiplexer security settings may disable it.

Press `b` to open the configured URL through the selected proxy in a disposable private browser session. If no `--url` or `URL` setting is configured, the browser opens a blank page so you can enter an address manually.

- Chrome/Chromium uses incognito mode and a temporary user-data directory.
- Firefox uses private mode and a generated temporary profile.
- The regular browser profile, cookies, history, and proxy settings are not modified.
- Only one browser session can be launched by one monitor at a time.
- Temporary profile data is removed after the launched browser closes.

`MONITOR_BROWSER` in `proxytools.conf` selects `auto`, `chrome`, or `firefox`. In `auto` mode, Chrome/Chromium is preferred and Firefox is used as a fallback.

Private browsing isolates the temporary session from the main profile. It is not a complete anonymity guarantee.

### Stop the monitor

Press `q` to stop. The monitor displays shutdown progress, allows active network requests to finish or reach their configured timeout, preserves useful monitor state, and then exits.

## About and credits

Run `./proxytools --about` from the shell or press `F1` in the monitor to see the project name, one-sentence description, current version, contributor list, and build date. Both interfaces use the same project metadata.

Until standalone release builds are introduced, the build date contains the project build year. A future release pipeline will replace it with the exact reproducible release-build date.

## Proxy statuses

### `PROBATION`

The proxy is still proving itself, recovering from failures, or has not yet met every configured stability condition. Typical blockers include insufficient live time, too few checks, an insufficient success rate, high latency, excessive jitter, or failure to reach the configured URL.

### `STABLE`

The proxy has remained available long enough and satisfies the required history, streak, success-rate, latency, and jitter thresholds.

A small number of isolated failures is tolerated. A previously stable proxy can recover after one clean complete check instead of repeating the entire initial probation period.

### `DEGRADED`

The proxy continued to fail after its recovery grace period. Degraded proxies are hidden by default but can be enabled with the `s` status filter.

### Restored marker `*`

A status ending in `*` was restored from saved monitor state and has not yet been verified during the current run. The asterisk is only a temporary marker, not a separate state.

## Understanding the measurements

- **Alive** — continuous healthy period used for stability admission; tolerated isolated failures do not reset it immediately.
- **Checks** — recent checks retained versus the number required for admission.
- **Streak** — consecutive complete successful checks.
- **OK** — complete success rate over the rolling history.
- **Median** — median of measured proxy latencies.
- **P95** — 95th-percentile measured latency.
- **Jitter** — deviation of measured latencies.
- **Country** — location resolved locally from the measured HTTPS exit IP.

One successful response is not enough to make a proxy stable. The monitor distinguishes:

- **reachable** means the proxy answered and latency was measured;
- **accepted** means the complete check passed, including latency and the optional target URL.

This is why a proxy can have a measured median and appear in the table while remaining in `PROBATION` or `DEGRADED`.

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
