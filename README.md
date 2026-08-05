# Proxy Tools

Three small CLI tools for working with free proxies from [ProxyScrape](https://proxyscrape.com/free-proxy-list), sharing a common library:

- **`proxylister.py`** — one-shot scan: fetch, check, geolocate, and save working proxies to a file. Optional HTTP preflight and Selenium validation against a real URL.
- **`proxymonitor.py`** — live dashboard: continuously re-scans and shows currently-working proxies in a color-coded terminal table. No file output.
- **`proxycountry.py`** — country breakdown of valid proxies, plus a fast country-list mode.
- **`proxylib.py`** — shared fetching/checking logic and typed result models. Not run directly.

## Requirements

- Python 3.10+
- `requests[socks]` (includes PySocks for `socks4`/`socks5` proxies)
- `selenium>=4.10` and Google Chrome — only needed for `proxylister.py --check-url`. Selenium Manager (bundled since 4.6) auto-downloads a matching ChromeDriver, so no manual driver setup is needed.
- `proxymonitor.py` uses the standard-library `curses` module. On Windows, install `windows-curses` first (`curses` isn't available there by default).

## Installation

```bash
python -m venv venv
source venv/bin/activate  # on Windows: venv\Scripts\activate

pip install -r requirements.txt
pip install -r requirements-selenium.txt  # only if you'll use --check-url
```

Keep the four Python files together in the same directory — all three CLI tools import from `proxylib.py`.

## proxylister.py

Fetches proxies for all protocols (`http`, `socks4`, `socks5`), dedupes them, checks which are alive concurrently, geolocates each, and writes the ones under `--max-latency` to a file, sorted fastest first.

```bash
python proxylister.py --timeout 5 --workers 50 --output working.txt --max-latency 500
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

## proxymonitor.py

Runs forever and keeps a rolling check history for each proxy. New candidates start in `PROBATION`, become `STABLE` only after satisfying every configured time and quality threshold, and change to `DEGRADED` after a failure. Green, yellow, and red rows represent those states. Nothing is written to disk.

```bash
python proxymonitor.py --timeout 5 --workers 50 --max-latency 500 --min-alive-time 60
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

**Controls:** `q` quits, `p` pauses/resumes the display (checks keep running in the background while paused).

The table is capped to what fits the terminal window, but hidden rows retain their history. It shows state, continuous live time, rolling success rate, median latency, p95 latency, jitter, country, and connection string. A proxy that disappears from ProxyScrape continues to be checked until `--retention-time` expires.

A successful check requires a duration below `--max-latency`. By default, any failed check resets continuous live time. Setting `--alive-failure-tolerance 1`, for example, preserves the original live-time counter through one isolated failure, although the proxy still becomes `DEGRADED` immediately.

## proxycountry.py

Print a country summary, or fetch only ProxyScrape's advertised country list without downloading and checking every proxy:

```bash
python proxycountry.py --timeout 5 --workers 50 --max-latency 500
python proxycountry.py --list-countries
```

## Tests

The standard-library test suite mocks all network access:

```bash
python -m unittest discover -v
```

## Notes

- Free proxies are short-lived and unreliable — expect a low success rate.
- `ip-api.com`'s free tier is rate-limited to 45 requests/minute per source IP, but since each lookup goes out through a different proxy, this rarely matters.
- Raise `--workers` for faster scans (more open connections), or `--timeout` on a slow connection.
- `--check-url` is slow by nature; raise `--browser-workers` carefully because each worker launches Chrome.
- On a headless server, pair `--check-url` with `--headless`, or Chrome will fail to launch.

## License

MIT License.
