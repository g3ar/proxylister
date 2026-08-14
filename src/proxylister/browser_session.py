"""Internal lifecycle helper for one disposable proxy browser session.

This is not a public CLI command. It runs outside the Textual process so a
browser may remain open after the monitor exits. Chrome receives an isolated
``--user-data-dir``; Firefox receives a generated profile containing only the
proxy preferences. The temporary directory is deleted after browser exit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def chrome_command(executable, profile, protocol, address, url):
    return [
        executable,
        "--incognito",
        f"--user-data-dir={profile}",
        f"--proxy-server={protocol}://{address}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]


def write_firefox_preferences(profile: Path, protocol: str, address: str):
    host, port_text = address.rsplit(":", 1)
    port = int(port_text)
    preferences = {"network.proxy.type": 1}
    if protocol == "http":
        preferences.update({
            "network.proxy.http": host,
            "network.proxy.http_port": port,
            "network.proxy.ssl": host,
            "network.proxy.ssl_port": port,
        })
    else:
        preferences.update({
            "network.proxy.socks": host,
            "network.proxy.socks_port": port,
            "network.proxy.socks_version": 4 if protocol == "socks4" else 5,
        })
    content = "".join(
        f"user_pref({json.dumps(name)}, {json.dumps(value)});\n"
        for name, value in preferences.items()
    )
    (profile / "user.js").write_text(content, encoding="utf-8")


def firefox_command(executable, profile, protocol, address, url):
    write_firefox_preferences(profile, protocol, address)
    return [executable, "-no-remote", "-profile", str(profile), "-private-window", url]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", required=True, choices=("chrome", "firefox"))
    parser.add_argument("--executable", required=True)
    parser.add_argument("--protocol", required=True, choices=("http", "socks4", "socks5"))
    parser.add_argument("--address", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="proxylister-browser-") as directory:
        profile = Path(directory)
        builder = chrome_command if args.family == "chrome" else firefox_command
        command = builder(args.executable, profile, args.protocol, args.address, args.url)
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
