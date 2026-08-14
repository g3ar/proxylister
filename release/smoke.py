#!/usr/bin/env python3
"""Cross-platform deterministic and bounded live smoke checks for frozen builds."""

from __future__ import annotations

import argparse
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, os.fspath(SCRIPT_DIR))

from buildlib.core import BuildError, run, tail  # noqa: E402


def _invoke(executable: Path, *arguments: str, env: dict[str, str]) -> str:
    result = run([executable, *arguments], env=env, capture=True)
    return result.stdout.strip()


def _clean_environment(home: Path) -> dict[str, str]:
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        return {
            "SystemRoot": system_root,
            "PATH": os.pathsep.join((str(Path(system_root) / "System32"), system_root)),
            "HOME": os.fspath(home),
            "USERPROFILE": os.fspath(home),
            "TEMP": os.environ.get("TEMP", tempfile.gettempdir()),
            "TMP": os.environ.get("TMP", tempfile.gettempdir()),
        }
    return {"HOME": os.fspath(home), "PATH": "/nonexistent", "LANG": "C.UTF-8"}


def _assert_contains(text: str, expected: str, label: str) -> None:
    if expected not in text:
        raise BuildError(f"{label} did not contain {expected!r}")


def _check_read_only(runtime: Path, smoke_root: Path, executable_name: str, env: dict[str, str]) -> None:
    read_only = smoke_root / "read-only"
    read_only.mkdir()
    executable = read_only / executable_name
    shutil.copy2(runtime / executable_name, executable)
    if os.name == "nt":
        run(["icacls.exe", read_only, "/deny", f"{os.environ['USERNAME']}:(WD,AD)"])
    else:
        read_only.chmod(0o555)
    try:
        result = run([executable, "list", "--help"], env=env, capture=True, check=False)
        if result.returncode == 0:
            raise BuildError("proxylister unexpectedly created a config in a read-only directory")
        _assert_contains(
            (result.stdout or "") + (result.stderr or ""),
            "configuration error: cannot create",
            "read-only failure",
        )
        if (read_only / "proxylister.conf").exists():
            raise BuildError("proxylister created a config in a read-only directory")
    finally:
        if os.name == "nt":
            run(["icacls.exe", read_only, "/remove:d", os.environ["USERNAME"], "/t", "/c"], check=False)
            run(["icacls.exe", read_only, "/reset", "/t", "/c"], check=False)
        else:
            read_only.chmod(0o755)


def offline_smoke(artifact: Path) -> None:
    artifact = artifact.resolve()
    if not artifact.is_file():
        raise BuildError(f"artifact is missing: {artifact}")
    executable_name = "proxylister.exe" if os.name == "nt" else "proxylister"
    with tempfile.TemporaryDirectory(prefix="proxylister-frozen-smoke-") as temporary:
        smoke_root = Path(temporary)
        runtime = smoke_root / "runtime"
        runtime.mkdir()
        executable = runtime / executable_name
        shutil.copy2(artifact, executable)
        if os.name != "nt":
            executable.chmod(0o755)
        env = _clean_environment(smoke_root / "home")
        version = _invoke(executable, "--version", env=env)
        if not version:
            raise BuildError("empty version output")
        about = _invoke(executable, "--about", env=env)
        _assert_contains(about, f"ProxyLister {version}", "about")
        _assert_contains(about, "Build date: 20", "about")
        _assert_contains(about, "Source commit: ", "about")
        _assert_contains(_invoke(executable, "--help", env=env), "Usage:", "root help")
        _assert_contains(
            _invoke(executable, "list", "--help", env=env), "--max-latency", "list help"
        )
        config = runtime / "proxylister.conf"
        if not config.is_file():
            raise BuildError("default config was not created")
        with config.open("a", encoding="utf-8") as stream:
            stream.write("\n# preserved by frozen smoke\n")
        _assert_contains(
            _invoke(executable, "monitor", "--help", env=env),
            "--max-latency",
            "monitor help",
        )
        _assert_contains(config.read_text(encoding="utf-8"), "# preserved by frozen smoke", "config")

        (runtime / "proxydb").mkdir()
        (runtime / "geodb").mkdir()
        (runtime / "geodb/version").write_text("generated\n", encoding="ascii")
        if os.name != "nt":
            import fcntl

            lock_stream = (runtime / "proxydb/proxylister.lock").open("w")
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = run([executable, "--clear"], env=env, capture=True, check=False)
            if locked.returncode == 0:
                raise BuildError("proxylister unexpectedly cleared state while its lock was held")
            _assert_contains(
                locked.stdout + locked.stderr,
                "refusing to clear: another proxylister process is already running",
                "locked clear",
            )
            lock_stream.close()
        clear = _invoke(executable, "--clear", env=env)
        if "Removed" not in clear and "already clean" not in clear:
            raise BuildError("clear smoke failed")
        for generated in (".venv", "geodb", "proxydb"):
            if (runtime / generated).exists():
                raise BuildError(f"clear retained generated path: {generated}")
        _check_read_only(runtime, smoke_root, executable_name, env)
    print(f"{platform_label()} frozen smoke tests passed.")


def platform_label() -> str:
    return "Windows" if os.name == "nt" else "Linux"


def _linux_monitor_smoke(executable: Path, log: Path, timeout: int) -> None:
    import pty

    master, slave = pty.openpty()
    with log.open("wb") as stream:
        process = subprocess.Popen([executable, "monitor"], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.write(master, b"q")
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None and time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.5)
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if chunk:
                        stream.write(chunk)
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=10)
            if process.returncode:
                raise BuildError(f"live monitor smoke failed with exit code {process.returncode}")
        finally:
            os.close(master)


def live_smoke(artifact: Path, *, list_timeout: int, monitor_timeout: int) -> None:
    artifact = artifact.resolve()
    if not artifact.is_file():
        raise BuildError(f"artifact is missing: {artifact}")
    root = SCRIPT_DIR.parent
    work_name = "windows" if os.name == "nt" else "local-linux"
    logs = root / "release/.work" / work_name / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stdout_log = logs / "live-list.log"
    stderr_log = logs / "live-list-error.log"
    for path in (stdout_log, stderr_log, logs / "live-monitor.log"):
        path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="proxylister-live-") as temporary:
        runtime = Path(temporary) / "runtime"
        runtime.mkdir()
        executable = runtime / ("proxylister.exe" if os.name == "nt" else "proxylister")
        shutil.copy2(artifact, executable)
        if os.name != "nt":
            executable.chmod(0o755)
        with stdout_log.open("w", encoding="utf-8") as stdout, stderr_log.open(
            "w", encoding="utf-8"
        ) as stderr:
            result = run(
                [executable, "list"],
                stdout=stdout,
                stderr=stderr,
                timeout=list_timeout,
                check=False,
            )
        if result.returncode:
            raise BuildError(
                f"live list smoke failed ({result.returncode}):\n"
                f"{tail(stdout_log, 120)}\n{tail(stderr_log, 120)}"
            )
        if not (runtime / "geodb/geoip.mmdb").is_file():
            raise BuildError("live smoke did not create the GeoIP database")
        if not (runtime / "working_proxies.txt").exists():
            raise BuildError("live smoke did not create working_proxies.txt")
        if os.name != "nt":
            _linux_monitor_smoke(executable, logs / "live-monitor.log", monitor_timeout)
            if not (runtime / "proxydb/proxylister.db").is_file():
                raise BuildError("live monitor smoke did not create proxylister.db")
    print(f"{platform_label()} live frozen smoke tests passed.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    offline = subparsers.add_parser("offline")
    offline.add_argument("artifact", type=Path)
    live = subparsers.add_parser("live")
    live.add_argument("artifact", type=Path)
    live.add_argument(
        "--list-timeout", type=int, default=int(os.environ.get("PROXYLISTER_LIVE_LIST_TIMEOUT", 300))
    )
    live.add_argument(
        "--monitor-timeout",
        type=int,
        default=int(os.environ.get("PROXYLISTER_LIVE_MONITOR_TIMEOUT", 60)),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.mode == "offline":
            offline_smoke(args.artifact)
        else:
            live_smoke(
                args.artifact,
                list_timeout=args.list_timeout,
                monitor_timeout=args.monitor_timeout,
            )
    except BuildError as exc:
        print(f"smoke: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
