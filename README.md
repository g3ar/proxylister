# Proxy Tools

Two small CLI tools for working with free proxies from [ProxyScrape](https://proxyscrape.com/free-proxy-list), sharing a common library:

- **`proxylister.py`** — one-shot scan: fetch, check, geolocate, and save working proxies to a file. Optional Selenium validation against a real URL.
- **`proxymonitor.py`** — live dashboard: continuously re-scans and shows currently-working proxies in a color-coded terminal table. No file output.
- **`proxylib.py`** — shared fetching/checking logic used by both. Not run directly.

## Requirements

- Python 3.9+
- `requests`, `requests[socks]` (PySocks, for `socks4`/`socks5` proxies)
- `selenium>=4.10` and Google Chrome — only needed for `proxylister.py --check-url`. Selenium Manager (bundled since 4.6) auto-downloads a matching ChromeDriver, so no manual driver setup is needed.
- `proxymonitor.py` uses the standard-library `curses` module. On Windows, install `windows-curses` first (`curses` isn't available there by default).

## Installation

```bash
python -m venv venv
source venv/bin/activate  # on Windows: venv\Scripts\activate

pip install requests requests[socks]
pip install "selenium>=4.10"   # only if you'll use --check-url
```

Keep `proxylib.py`, `proxylister.py`, and `proxymonitor.py` together in the same directory — the other two import from `proxylib.py`.

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
| `--max-latency` | Only keep proxies faster than this (ms) | `500` |
| `--check-url` | URL to validate each fast proxy against via Selenium (see below) | disabled |
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

The moment a proxy passes the latency filter, it's opened in Chrome through that proxy — no separate pass, this happens inline during the scan. A proxy is dropped if the page fails to load, if Chrome shows its own internal network-error page, or if the response itself is an HTTP error (e.g. a dead proxy returning its own `502`/`403` page instead of forwarding to the target). On success the window stays open 10 seconds (skipped in `--headless` mode) so you can visually confirm the page actually rendered, then moves to the next proxy. The Selenium page-load timeout is `2 × --max-latency` in seconds, floored at 10s.

## proxymonitor.py

Runs forever: each cycle re-fetches a fresh proxy list, checks it, and updates a live curses table of every currently-valid proxy (latency, protocol, country, last-checked time, connection string), color-coded green/yellow/red by how close it is to `--max-latency`. Nothing is written to disk.

```bash
python proxymonitor.py --timeout 5 --workers 50 --max-latency 500
```

| Flag | Description | Default |
|------|-------------|---------|
| `--timeout` | Seconds to wait per proxy check | `5` |
| `--workers` | Concurrent worker threads | `50` |
| `--max-latency` | Only track proxies faster than this (ms) | `500` |

**Controls:** `q` quits, `p` pauses/resumes the display (checks keep running in the background while paused).

The table is capped to whatever fits the terminal window at startup (resizing afterward has no effect); if more proxies qualify than fit, the slowest ones are dropped first. A proxy that stops passing on a re-check is removed immediately.

## Notes

- Free proxies are short-lived and unreliable — expect a low success rate.
- `ip-api.com`'s free tier is rate-limited to 45 requests/minute per source IP, but since each lookup goes out through a different proxy, this rarely matters.
- Raise `--workers` for faster scans (more open connections), or `--timeout` on a slow connection.
- `--check-url` is slow by nature (~10+ seconds per proxy, one browser at a time) — the more proxies pass `--max-latency`, the longer the run takes.
- On a headless server, pair `--check-url` with `--headless`, or Chrome will fail to launch.

## License

MIT License.
