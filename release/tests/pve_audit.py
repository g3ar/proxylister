#!/usr/bin/env python3
"""Verify that Python template audits leave persistent PVE build-lab state unchanged."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[1]
ROOT = RELEASE.parent
sys.path.insert(0, os.fspath(RELEASE))

from buildlib.core import BuildError, LEGACY_PVE_LOCK_PATH, run  # noqa: E402
from buildlib.pve import SSHPVE, SSHConfig  # noqa: E402


def snapshot(backend: SSHPVE, target: str) -> str:
    paths = ["/etc/pve/storage.cfg"]
    state_directories = ["/var/lib/vz/template/iso"]
    vmids = ["9000", "9001"] if target == "linux" else ["9002"]
    if target == "linux":
        paths.insert(0, "/etc/lvm/lvm.conf")
        state_directories.append("/var/lib/vz/snippets")
    script = (
        "import hashlib,os,pathlib;"
        f"paths={paths!r};"
        f"directories={state_directories!r};"
        "[(lambda p:print('file',p,hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest(),"
        "os.stat(p).st_mode,os.stat(p).st_size,os.stat(p).st_mtime_ns))(p) for p in paths];"
        "[(lambda d:print('dir',d,[(e.name,e.stat().st_mode,e.stat().st_size,e.stat().st_mtime_ns) "
        "for e in sorted(os.scandir(d),key=lambda x:x.name)]))(d) for d in directories];"
        f"p={os.fspath(LEGACY_PVE_LOCK_PATH)!r};"
        "print('lock',pathlib.Path(p).read_text() if os.path.exists(p) else 'absent',"
        "os.stat(p).st_mtime_ns if os.path.exists(p) else 0)"
    )
    parts = [backend.command(["python3", "-c", script]), backend.command(["qm", "list"])]
    parts.extend(backend.command(["qm", "config", vmid]) for vmid in vmids)
    if target == "linux":
        parts.append(backend.command(["systemctl", "is-active", "lvm2-monitor.service"]))
    return "\n---\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("linux", "windows"))
    args = parser.parse_args()
    work = ROOT / "release/.work/pve-audit"
    config = SSHConfig.from_environment(work)
    backend = SSHPVE(config)
    before = snapshot(backend, args.target)
    run([sys.executable, RELEASE / "build.py", "provision", args.target, "--check-only"], cwd=ROOT)
    after = snapshot(backend, args.target)
    if before != after:
        raise BuildError(f"{args.target} --check-only changed persistent build-lab state")
    print(f"PVE {args.target} provision read-only test: OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"pve-audit: {exc}", file=sys.stderr)
        raise SystemExit(1)
