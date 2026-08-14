#!/usr/bin/env python3
"""One cross-platform entrypoint for ProxyLister builds and build-lab operations."""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, os.fspath(SCRIPT_DIR))

from buildlib.core import BuildError, eprint, require_commands  # noqa: E402
from buildlib.native import build_native  # noqa: E402
from buildlib.pipelines import (  # noqa: E402
    local_linux_build,
    pve_linux_build,
    pve_windows_build,
)
from buildlib.pve import PVELock, SSHPVE, SSHConfig  # noqa: E402
from buildlib.source import create_snapshot  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="{build,provision}"
    )

    build = commands.add_parser("build", help="build and validate a native executable")
    build.add_argument("target", choices=("linux", "windows", "all"))
    build.add_argument(
        "--pve",
        action="store_true",
        help="run in disposable PVE clones instead of the local machine",
    )

    provision = commands.add_parser(
        "provision", help="create or audit immutable templates on the configured PVE host"
    )
    provision.add_argument("target", choices=("linux", "windows"))
    provision.add_argument("--check-only", action="store_true")
    provision.add_argument("--ssh-public-key", type=Path)
    build.add_argument(
        "--release",
        action="store_true",
        help="require a clean tree and build one exact archive from HEAD",
    )

    native = commands.add_parser("_native")
    native.add_argument("target", choices=("linux", "windows"))
    native.add_argument("--source-commit", required=True)
    native.add_argument("--source-tree", required=True, choices=("clean", "dirty"))
    host = commands.add_parser("_provision_host")
    host.add_argument("target", choices=("linux", "windows"))
    host.add_argument("--check-only", action="store_true")
    host.add_argument("--ssh-public-key", type=Path)
    return parser


def _pve_build(target: str, release: bool) -> list[Path]:
    require_commands(("git", "scp", "ssh", "ssh-keygen"))
    control_work = ROOT / "release/.work/pve-control"
    config = SSHConfig.from_environment(control_work)
    config.validate()
    backend = SSHPVE(config)
    artifacts: list[Path] = []
    with PVELock(backend):
        snapshot = create_snapshot(ROOT, control_work, release=release)
        if target in {"linux", "all"}:
            artifacts.append(pve_linux_build(ROOT, snapshot))
        if target in {"windows", "all"}:
            artifacts.append(pve_windows_build(ROOT, snapshot))
    return artifacts


def _build(args: argparse.Namespace) -> int:
    if args.release and not args.pve:
        raise BuildError("--release is supported only for a PVE build")
    if not args.pve and args.target != "linux":
        raise BuildError("Windows and all-platform builds require --pve")
    if args.pve:
        artifacts = _pve_build(args.target, args.release)
    else:
        require_commands(("git",))
        snapshot = create_snapshot(ROOT, ROOT / "release/.work/local-source", release=False)
        artifacts = [local_linux_build(ROOT, snapshot)]
    for artifact in artifacts:
        print(f"Artifact: {artifact}")
    return 0


def _scp_to_pve(config: SSHConfig, source: Path, destination: str) -> None:
    from buildlib.core import run

    run(
        [
            "scp",
            "-F",
            os.devnull,
            "-i",
            config.pve_key,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=20",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={config.pve_known_hosts}",
            source,
            f"{config.pve_host}:{destination}",
        ]
    )


def _provision_remote(args: argparse.Namespace) -> int:
    require_commands(("scp", "ssh"))
    work = ROOT / "release/.work/pve-provision"
    work.mkdir(parents=True, exist_ok=True)
    config = SSHConfig.from_environment(work)
    if not config.pve_key.is_file():
        raise BuildError(f"PVE root key not found: {config.pve_key}")
    if not config.pve_known_hosts.is_file():
        raise BuildError(f"PVE known-hosts file not found: {config.pve_known_hosts}")
    public_key = args.ssh_public_key
    if not args.check_only and public_key is None:
        public_key = Path(f"{config.guest_key}.pub")
    if public_key is not None and not public_key.is_file():
        raise BuildError(f"guest public key not found: {public_key}")
    backend = SSHPVE(config)
    remote = f"/tmp/proxylister-python-provision-{os.getpid()}"
    bundle = work / "provision.tar"
    with tarfile.open(bundle, "w") as archive:
        archive.add(SCRIPT_DIR / "build.py", arcname="release/build.py")
        archive.add(
            SCRIPT_DIR / "build_config.py",
            arcname="release/build_config.py",
        )
        archive.add(SCRIPT_DIR / "buildlib", arcname="release/buildlib")
        if args.target == "windows":
            archive.add(
                SCRIPT_DIR / "pve/windows/autounattend.xml",
                arcname="release/pve/windows/autounattend.xml",
            )
            archive.add(
                SCRIPT_DIR / "pve/windows/bootstrap.ps1",
                arcname="release/pve/windows/bootstrap.ps1",
            )
    backend.command(["mkdir", "-p", remote])
    try:
        _scp_to_pve(config, bundle, f"{remote}/provision.tar")
        remote_key: str | None = None
        if public_key is not None:
            remote_key = f"{remote}/proxylister-build.pub"
            _scp_to_pve(config, public_key, remote_key)
        # Keep extraction arguments structured; only the Python snippet is one remote argument.
        backend.command(
            [
                "python3",
                "-c",
                f"import tarfile;tarfile.open('{remote}/provision.tar').extractall('{remote}')",
            ]
        )
        command = [
            "python3",
            f"{remote}/release/build.py",
            "_provision_host",
            args.target,
        ]
        if args.check_only:
            command.append("--check-only")
        if remote_key is not None:
            command.extend(("--ssh-public-key", remote_key))
        backend.stream_command(command)
    finally:
        backend.command(
            [
                "python3",
                "-c",
                f"import shutil;shutil.rmtree('{remote}',ignore_errors=True)",
            ],
            check=False,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            return _build(args)
        if args.command == "provision":
            return _provision_remote(args)
        if args.command == "_provision_host":
            from buildlib.provision import provision_host

            provision_host(
                args.target,
                ROOT / "release/pve/windows",
                args.ssh_public_key,
                args.check_only,
            )
            return 0
        artifact = build_native(ROOT, args.target, args.source_commit, args.source_tree)
        print(f"Artifact: {artifact}")
        return 0
    except (BuildError, OSError) as exc:
        eprint(f"build: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
