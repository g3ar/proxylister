# proxylister

A simple Python CLI tool that fetches free proxies from [ProxyScrape](https://proxyscrape.com/free-proxy-list), checks which ones are alive, geolocates each working proxy's exit IP, and optionally verifies each fast proxy by loading a real page through it with Selenium — right as that proxy is found valid.

## Features

- Fetches proxies for all protocols — `http`, `socks4`, `socks5` — via ProxyScrape's public API
- Checks proxy availability concurrently using a thread pool
- Geolocates each working proxy's exit IP (country + GPS coordinates) via [ip-api.com](http://ip-api.com)
- Outputs only working proxies, sorted by latency, filtered to only proxies faster than a given threshold, with a ready-to-use browser connection string and a Google Maps link
- Optional Selenium validation (`--check-url`): the moment a proxy passes the latency filter, opens a real page through it in a Chrome browser, confirming it actually renders content (not just that it responds to a bare HTTP request) before it's kept in the output

## Requirements

- Python 3.9+
- `requests`
- `requests[socks]` (PySocks) — required to check `socks4`/`socks5` proxies
- `selenium>=4.10` — only needed if you use `--check-url`
- Google Chrome installed — only needed if you use `--check-url`. Selenium 4.10+ includes Selenium Manager, which automatically downloads a matching ChromeDriver, so no separate driver install or PATH setup is needed.

## Installation

```bash
cd proxylister

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # on Windows: venv\Scripts\activate

# Install dependencies
pip install requests requests[socks]
```

### Selenium setup (only needed for `--check-url`)

```bash
pip install "selenium>=4.10"
```

You also need **Google Chrome** installed on the machine running the script — Selenium drives the actual Chrome browser, it doesn't bundle one.

- **Linux**: install `google-chrome-stable` from Google's apt/yum repo, or your distro's Chromium package.
- **macOS**: `brew install --cask google-chrome`, or download from [google.com/chrome](https://www.google.com/chrome/).
- **Windows**: download and install from [google.com/chrome](https://www.google.com/chrome/).

No manual ChromeDriver download or `PATH` setup is required — Selenium Manager (bundled since Selenium 4.6) detects your installed Chrome version and downloads a matching driver automatically the first time `--check-url` runs.

If you're running on a headless server with no display, pair `--check-url` with `--headless` (see Options below) so Chrome doesn't need a windowing system.

## Usage

```bash
python proxylister.py [options]
```

### Example

```bash
python proxylister.py --timeout 5 --workers 50 --output working.txt --max-latency 500
```

This fetches http, socks4, and socks5 proxies, tests each with a 5-second timeout using 50 concurrent workers, and saves only proxies faster than 500ms to `working.txt`. `--max-latency` defaults to `500` even if omitted; pass a different value to change the threshold.

```bash
python proxylister.py --max-latency 500 --check-url https://example.com
```

Same as above, but as soon as a proxy is found valid (latency under 500ms), it's immediately opened in a visible Chrome window through that proxy — any proxy whose page fails to load is dropped, and Selenium checks every valid proxy found, one at a time.

```bash
python proxylister.py --max-latency 500 --check-url https://example.com --headless
```

Same again, but Selenium runs headlessly instead of showing a visible window — use this on a server with no display.

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--timeout` | Seconds to wait per proxy check | `5` |
| `--workers` | Number of concurrent worker threads | `50` |
| `--output` | File to save working proxies to | `working_proxies.txt` |
| `--max-latency` | Only keep proxies with latency lower than this (ms). Also determines the Selenium page-load timeout (`2x` this value, in seconds). | `500` |
| `--check-url` | URL to open via Selenium through each proxy the moment it's found valid, as an extra validation layer | none (layer disabled) |
| `--headless` | Run `--check-url` browser checks headlessly instead of in a visible window | off (visible window) |

## CLI Output

While proxies are being checked, the CLI shows a single live progress bar (no per-proxy lines):

```
[########################----------------] 60% (300/500, 42 working)
```

It updates in place as each check completes, then prints a final summary and the output file path.

Press **Ctrl+C** at any time to stop early — including mid-Selenium-check if `--check-url` is running. In-progress checks are dropped, and only the proxies already confirmed working (and, if applicable, already Selenium-verified) at that point are written to the output file — nothing partial or unverified gets saved.

## File Output Format

Confirmed-working results are written to the output file once the script stops, sorted by latency from low to high, one proxy per line. Only proxies with lower latency than `--max-latency` (default `500`ms) are included:

```
<latency> protocol server:port <connection string> <country> <lat,lon> <google maps link>
```

Example:

```
842ms socks5 62.133.62.207:1081 socks5://62.133.62.207:1081 Germany 51.2993,9.491 https://www.google.com/maps?q=51.2993,9.491
```

- **latency** — round-trip time of the check request (`response.elapsed`), i.e. pure network time: proxy connect + handshake + forward + reply, with no local JSON-parsing or scheduling overhead included
- **connection string** — ready to paste into a browser's or OS's proxy settings
- **country / lat,lon** — geolocation of the proxy's exit IP
- **google maps link** — opens that location directly on Google Maps

Dead or unreachable proxies are silently skipped — only working ones appear in the output.

## Selenium URL Validation (`--check-url`)

When `--check-url <URL>` is set, Selenium validation happens **inline**, per proxy, the instant that proxy passes the latency filter during the network scan — not as a separate pass afterward:

1. As soon as a proxy's latency comes in under `--max-latency`, Chrome is launched configured to route traffic through that one proxy (`--proxy-server=<protocol>://ip:port` — works for `http`, `socks4`, and `socks5` alike).
2. Navigates to `--check-url` and waits for it to finish loading, up to a timeout of `2 × --max-latency` (in seconds) — e.g. the default `--max-latency 500` gives Selenium a 1-second page-load timeout. A proxy fast enough to pass the latency filter is expected to load a page within a couple multiples of that same latency; if it can't, that's itself a sign the proxy is unreliable.
3. **On success**: holds the browser window open for 10 seconds (so you can visually confirm the page actually rendered — useful for catching proxies that pass a bare connectivity check but serve broken pages, interstitials, or CAPTCHAs), then closes it and the network scan continues to the next proxy.
4. **On failure**: closes the browser immediately with no wait, and the proxy is dropped — it will not appear in the output file. "Failure" covers three cases: a Selenium/timeout exception, Chrome silently rendering its own internal network-error page (connection refused, proxy tunnel failure, DNS failure, etc. — this doesn't raise an exception on its own, so the script explicitly checks for it), and the main document response itself being an HTTP error (many broken/free proxies respond with their own valid-looking `502`/`403`/`504`/etc. page instead of forwarding to the target — the script reads Chrome's network log to catch this too, since it also looks like a normal successful page load otherwise).

Because this runs inline, the network scan (still concurrent across all proxies) and Selenium checks (one browser at a time, sequential) overlap — Selenium starts working through valid proxies as they're discovered rather than waiting for the entire scan to finish first. Every valid proxy gets checked; there's no early-stop count.

The CLI prints a `checking <url> via <proxy>` line each time Selenium picks up a newly valid proxy, so you can watch progress alongside the main scan's progress bar.

If Ctrl+C is pressed — whether during the network scan or in the middle of a Selenium check — the script stops immediately: any open browser is closed first, then the run ends. Only proxies already verified before the interrupt are saved.

## How It Works

1. Fetches raw proxy lists (one per protocol) from ProxyScrape's API and extracts `ip:port` pairs.
2. For each proxy, concurrently makes a single request through it to `ip-api.com`, which both confirms the proxy is alive, returns the geolocation of its exit IP, and times the round trip to record as network latency.
3. The instant a proxy's latency comes in under `--max-latency`, if `--check-url` is set, that one proxy is immediately validated with Selenium (see below) before the scan moves on — checks aren't batched into a separate phase.
4. Updates a live progress bar in the CLI as checks (and any inline Selenium checks) complete.
5. Once the scan finishes (or is interrupted), sorts confirmed-working results by latency, filters out anything at or above `--max-latency`, and writes that list to the output file.
6. If `--check-url` was set, also writes a second pass to the same output file containing only the proxies that passed the Selenium check.

## Project Structure

```
proxylister/
├── proxylister.py   # Main script
└── README.md
```

## Notes

- Free proxies are often short-lived and unreliable, so expect a low success rate.
- `ip-api.com`'s free tier is HTTP only and rate-limited to 45 requests/minute per source IP — since each lookup goes out through a different proxy IP, this limit rarely comes into play.
- Increase `--timeout` if you're on a slow connection, or `--workers` for faster checking (at the cost of more open connections).
- `--max-latency` defaults to `500`ms; pass a different value to widen or narrow what counts as "fast" for your use case.
- `--check-url` is slow by nature (one browser, one proxy, ~10+ seconds each) and runs inline as valid proxies are found — the more proxies pass your `--max-latency` threshold, the longer the total run takes, since every one of them gets a Selenium check.
- The Selenium page-load timeout isn't separately configurable; it's always `2 × --max-latency` in seconds. Widening `--max-latency` therefore also gives Selenium more time per page load.
- On a headless server (no display), always pair `--check-url` with `--headless`, or Chrome will fail to launch.

## License

MIT License.