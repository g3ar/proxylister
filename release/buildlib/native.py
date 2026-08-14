"""Native PyInstaller build shared by Linux and Windows guests."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import (
    BuildError,
    platform_python,
    promote_directory,
    remove_tree,
    run,
    sha256,
    write_checksums,
)


def _constraints(root: Path, target: str) -> Path:
    if target == "linux":
        return root / "release/linux/constraints.txt"
    return root / "release/pve/windows/constraints.txt"


def _work_name(target: str) -> str:
    return "local-linux" if target == "linux" else "windows"


def _run_logged(
    args: list[object],
    log: Path,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"+ {subprocess.list2cmdline([os.fspath(arg) for arg in args])}\n")
        stream.flush()
        run(args, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)


def _resolved_packages(python: Path, env: dict[str, str], log: Path) -> list[str]:
    result = run(
        [python, "-m", "pip", "freeze", "--all", "--exclude-editable"],
        env=env,
        capture=True,
    )
    with log.open("a", encoding="utf-8") as stream:
        stream.write(result.stdout)
    return sorted(line.strip().lower() for line in result.stdout.splitlines() if line.strip())


def _os_description(target: str) -> str:
    if target == "windows":
        return platform.platform()
    values: dict[str, str] = {}
    os_release = Path("/etc/os-release")
    if os_release.is_file():
        for line in os_release.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip('"')
    return values.get("PRETTY_NAME", platform.platform())


def build_native(root: Path, target: str, source_commit: str, source_tree: str) -> Path:
    if target not in {"linux", "windows"}:
        raise BuildError(f"unsupported native build target: {target}")
    if source_tree not in {"clean", "dirty"}:
        raise BuildError(f"invalid source tree state: {source_tree}")
    work = root / "release/.work" / _work_name(target)
    bin_root = root / "release/bin"
    destination = bin_root / target
    venv = work / "venv"
    dist = work / "artifacts"
    pyinstaller_work = work / "pyinstaller"
    logs = work / "logs"
    constraints = _constraints(root, target)
    artifact_name = "proxylister.exe" if target == "windows" else "proxylister"

    remove_tree(work)
    remove_tree(destination)
    for directory in (dist, pyinstaller_work, logs, bin_root):
        directory.mkdir(parents=True, exist_ok=True)
    build_log = logs / "build.log"
    build_log.write_text("", encoding="utf-8")

    base_python = Path(sys.executable)
    _run_logged([base_python, "-m", "venv", venv], build_log)
    python = platform_python(venv)
    pyinstaller = venv / ("Scripts/pyinstaller.exe" if target == "windows" else "bin/pyinstaller")
    env = os.environ.copy()
    env["PIP_CONSTRAINT"] = os.fspath(constraints)
    _run_logged([python, "-m", "pip", "install", "--upgrade", "pip", "setuptools"], build_log, env=env)
    _run_logged([python, "-m", "pip", "install", "-e", root, "pyinstaller"], build_log, env=env)

    expected = sorted(
        line.strip().lower()
        for line in constraints.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    resolved = _resolved_packages(python, env, build_log)
    (work / "expected-packages.txt").write_text("\n".join(expected) + "\n", encoding="utf-8")
    (work / "resolved-packages.txt").write_text("\n".join(resolved) + "\n", encoding="utf-8")
    if expected != resolved:
        missing = sorted(set(expected) - set(resolved))
        extra = sorted(set(resolved) - set(expected))
        raise BuildError(f"resolved packages differ from constraints (missing={missing}, extra={extra})")

    _run_logged([python, "-m", "unittest", "discover", "-v"], build_log, cwd=root, env=env)
    compile_script = (
        "import pathlib,py_compile;"
        "paths=[pathlib.Path('proxylister'),*pathlib.Path('src/proxylister').rglob('*.py')];"
        "[py_compile.compile(str(p),doraise=True) for p in paths]"
    )
    _run_logged([python, "-c", compile_script], build_log, cwd=root, env=env)

    build_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    build_info = work / "proxylister-build.txt"
    build_info.write_text(
        f"build_utc={build_utc}\nsource_commit={source_commit}\n", encoding="ascii"
    )
    env["PROXYLISTER_BUILD_INFO"] = os.fspath(build_info)
    _run_logged(
        [
            pyinstaller,
            "--noconfirm",
            "--clean",
            "--distpath",
            dist,
            "--workpath",
            pyinstaller_work,
            root / "release/pyinstaller/proxylister.spec",
        ],
        build_log,
        cwd=root,
        env=env,
    )

    shutil.copy2(root / "README.md", dist / "README.md")
    shutil.copy2(root / "LICENSE", dist / "LICENSE")
    artifact = dist / artifact_name
    if not artifact.is_file():
        raise BuildError(f"PyInstaller did not create {artifact_name}")
    if target == "linux":
        artifact.chmod(0o755)
    version = run([artifact, "--version"], capture=True).stdout.strip()
    if not version or any(not (char.isalnum() or char in ".-") for char in version):
        raise BuildError(f"invalid artifact version: {version}")
    pip_version = run([python, "-m", "pip", "--version"], capture=True).stdout.split()[1]
    pyinstaller_version = run([pyinstaller, "--version"], capture=True).stdout.strip()
    manifest = [
        f"artifact={artifact_name}",
        f"version={version}",
        f"source_commit={source_commit}",
        f"source_tree={source_tree}",
        f"build_utc={build_utc}",
        f"os={_os_description(target)}",
        f"architecture={platform.machine()}",
        f"python={run([python, '--version'], capture=True).stdout.strip()}",
        f"pip={pip_version}",
        f"pyinstaller={pyinstaller_version}",
        f"constraints_sha256={sha256(constraints)}",
    ]
    (dist / "MANIFEST.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    write_checksums(dist, [artifact_name, "README.md", "LICENSE", "MANIFEST.txt"])

    smoke_log = logs / "smoke.log"
    with smoke_log.open("w", encoding="utf-8") as stream:
        run(
            [base_python, root / "release/smoke.py", "offline", artifact],
            cwd=root,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    promote_directory(dist, destination)
    return destination / artifact_name
