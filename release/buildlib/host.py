"""Primitives used while provisioning templates directly on the PVE host."""

from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Sequence

from .core import BuildError, LEGACY_PVE_LOCK_PATH, run


class LocalPVE:
    def command(self, args: Sequence[str], *, check: bool = True) -> str:
        result = run(args, capture=True, check=False)
        if result.returncode:
            if check:
                details = (result.stderr or result.stdout).strip()
                raise BuildError(f"PVE command failed: {' '.join(args)}\n{details}")
            return "__FAILED__"
        return result.stdout.strip()


class HostLock:
    def __init__(self, path: Path = LEGACY_PVE_LOCK_PATH):
        self.path = path
        self.stream: object | None = None

    def __enter__(self) -> "HostLock":
        import fcntl

        stream = self.path.open("w", encoding="ascii")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise BuildError("another PVE build/provision operation owns the build-lab lock") from exc
        stream.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        stream.flush()
        self.stream = stream
        return self

    def __exit__(self, *_: object) -> None:
        if self.stream is not None:
            self.stream.close()  # type: ignore[union-attr]
            self.stream = None


def require_root() -> None:
    if os.geteuid() != 0:
        raise BuildError("run template provisioning as root on the PVE host")


def guest_command_ok(output: str) -> bool:
    return bool(re.search(r'"exited"\s*:\s*1', output)) and bool(
        re.search(r'"exitcode"\s*:\s*0', output)
    )


def guest_shell(backend: LocalPVE, vmid: int, script: str) -> None:
    result = backend.command(["qm", "guest", "exec", str(vmid), "--", "/bin/sh", "-c", script])
    if not guest_command_ok(result):
        raise BuildError(f"guest command failed in VMID {vmid}:\n{result}")


def guest_powershell(backend: LocalPVE, vmid: int, script: str, *, check: bool = True) -> bool:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = backend.command(
        [
            "qm",
            "guest",
            "exec",
            str(vmid),
            "--",
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        check=check,
    )
    ok = result != "__FAILED__" and guest_command_ok(result)
    if check and not ok:
        raise BuildError(f"Windows guest command failed in VMID {vmid}:\n{result}")
    return ok
