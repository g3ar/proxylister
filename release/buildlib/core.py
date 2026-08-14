"""Small process, filesystem, and checksum primitives used by build tooling."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import IO, Iterable, Mapping, Sequence


class BuildError(RuntimeError):
    """A user-facing build or infrastructure failure."""


# Every build and provisioning operation contends on the same host lock.
PVE_LOCK_PATH = Path("/run/lock/proxylister-pve-build.lock")


def command_text(args: Sequence[object]) -> str:
    return subprocess.list2cmdline([os.fspath(arg) for arg in args])


def run(
    args: Sequence[object],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    capture: bool = False,
    check: bool = True,
    timeout: float | None = None,
    stdout: int | IO[bytes] | IO[str] | None = None,
    stderr: int | IO[bytes] | IO[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [os.fspath(arg) for arg in args]
    if capture and (stdout is not None or stderr is not None):
        raise ValueError("capture cannot be combined with explicit output streams")
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE if capture else stdout,
            stderr=subprocess.PIPE if capture else stderr,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BuildError(f"required command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"command timed out after {timeout}s: {command_text(argv)}") from exc
    if check and result.returncode:
        details = ""
        if capture:
            details = (result.stderr or result.stdout or "").strip()
        suffix = f"\n{details}" if details else ""
        raise BuildError(
            f"command failed with exit code {result.returncode}: {command_text(argv)}{suffix}"
        )
    return result


def output(args: Sequence[object], **kwargs: object) -> str:
    result = run(args, capture=True, **kwargs)
    return result.stdout.strip()


def require_commands(names: Iterable[str]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise BuildError(f"required command not found: {', '.join(missing)}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(directory: Path, names: Sequence[str]) -> None:
    lines = [f"{sha256(directory / name)}  {name}\n" for name in names]
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="ascii")


def verify_checksums(directory: Path) -> None:
    checksum_file = directory / "SHA256SUMS"
    if not checksum_file.is_file():
        raise BuildError(f"checksum file is missing: {checksum_file}")
    for raw_line in checksum_file.read_text(encoding="ascii").splitlines():
        fields = raw_line.split(None, 1)
        if len(fields) != 2:
            raise BuildError(f"invalid checksum line: {raw_line}")
        expected, name = fields
        name = name.lstrip("*").strip()
        target = directory / name
        if not target.is_file() or sha256(target) != expected.lower():
            raise BuildError(f"artifact checksum mismatch: {target}")


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def promote_directory(source: Path, destination: Path) -> None:
    """Replace one platform output without touching its sibling platform."""
    if not source.is_dir():
        raise BuildError(f"artifact directory is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    remove_tree(destination)
    source.replace(destination)


def tail(path: Path, lines: int = 160) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def platform_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
