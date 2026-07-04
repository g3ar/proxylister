# proxylister

A simple Python CLI tool that fetches free proxies from [ProxyScrape](https://proxyscrape.com/free-proxy-list), checks which ones are alive, and geolocates each working proxy's exit IP.

## Features

- Fetches proxies for all protocols — `http`, `socks4`, `socks5` — via ProxyScrape's public API
- Checks proxy availability concurrently using a thread pool
- Geolocates each working proxy's exit IP (country + GPS coordinates) via [ip-api.com](http://ip-api.com)
- Outputs only working proxies, both to the CLI and to a file, with a ready-to-use browser connection string and a Google Maps link

## Requirements

- Python 3.9+
- `requests`
- `requests[socks]` (PySocks) — required to check `socks4`/`socks5` proxies

## Installation

```bash
cd proxylister

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # on Windows: venv\Scripts\activate

# Install dependencies
pip install requests requests[socks]
```

## Usage

```bash
python proxylister.py [options]
```

### Example

```bash
python proxylister.py --timeout 5 --workers 50 --output working.txt
```

This fetches http, socks4, and socks5 proxies, tests each with a 5-second timeout using 50 concurrent workers, and saves the working ones to `working.txt`.

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--timeout` | Seconds to wait per proxy check | `5` |
| `--workers` | Number of concurrent worker threads | `50` |
| `--output` | File to save working proxies to | `working_proxies.txt` |

## CLI Output

While proxies are being checked, the CLI shows a single live progress bar (no per-proxy lines):

```
[########################----------------] 60% (300/500, 42 working)
```

It updates in place as each check completes, then prints a final summary and the output file path.

Press **Ctrl+C** at any time to stop early. In-progress checks are dropped, and only the proxies already confirmed working at that point are written to the output file — nothing partial or unverified gets saved.

## File Output Format

Detailed results are written only to the output file, one working proxy per line:

```
protocol server:port <connection string> <country> <lat,lon> <google maps link>
```

Example:

```
socks5 62.133.62.207:1081 socks5://62.133.62.207:1081 Germany 51.2993,9.491 https://www.google.com/maps?q=51.2993,9.491
```

- **connection string** — ready to paste into a browser's or OS's proxy settings
- **country / lat,lon** — geolocation of the proxy's exit IP
- **google maps link** — opens that location directly on Google Maps

Dead or unreachable proxies are silently skipped — only working ones appear in the output.

## How It Works

1. Fetches raw proxy lists (one per protocol) from ProxyScrape's API and extracts `ip:port` pairs.
2. For each proxy, makes a single request through it to `ip-api.com`, which both confirms the proxy is alive and returns the geolocation of its exit IP.
3. Updates a live progress bar in the CLI as checks complete.
4. Writes all confirmed-working proxy lines, with full detail, to the output file — whether the run finishes normally or is stopped early with Ctrl+C.

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

## License

MIT License.