"""Shared Proxmox, SSH, clone-lifecycle, and destructive safety operations."""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence

from build_config import PVE_HOST

from .core import BuildError, PVE_LOCK_PATH, command_text, run


MEDIA_SLOT = re.compile(r"^(?:ide|sata|scsi|virtio|unused)\d+$")
IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


class PVECommand(Protocol):
    def command(self, args: Sequence[str], *, check: bool = True) -> str: ...


def configured_pve_destination() -> str:
    """Return the configured build host with the mandatory root SSH user."""
    host = PVE_HOST.strip()
    if (
        not host
        or host != PVE_HOST
        or "@" in host
        or any(char.isspace() for char in host)
    ):
        raise BuildError(
            "release/build_config.py PVE_HOST must contain only a hostname or IP address"
        )
    return f"root@{host}"


@dataclass(frozen=True)
class SSHConfig:
    pve_host: str
    pve_key: Path
    guest_key: Path
    pve_known_hosts: Path
    guest_known_hosts: Path

    @classmethod
    def from_environment(cls, work: Path) -> "SSHConfig":
        home = Path.home()
        return cls(
            pve_host=configured_pve_destination(),
            pve_key=Path(
                os.environ.get("PROXYLISTER_PVE_ROOT_KEY", home / ".ssh/id_rsa")
            ).expanduser(),
            guest_key=Path(
                os.environ.get(
                    "PROXYLISTER_PVE_GUEST_KEY", home / ".ssh/proxylister-build"
                )
            ).expanduser(),
            pve_known_hosts=Path(
                os.environ.get(
                    "PROXYLISTER_PVE_KNOWN_HOSTS", home / ".ssh/known_hosts"
                )
            ).expanduser(),
            guest_known_hosts=work / "known_hosts",
        )

    def validate(self) -> None:
        for label, path in (("PVE root", self.pve_key), ("guest", self.guest_key)):
            if not path.is_file():
                raise BuildError(f"{label} key not found: {path}")
        if not self.pve_known_hosts.is_file():
            raise BuildError(f"PVE known-hosts file not found: {self.pve_known_hosts}")
        self.guest_known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self.guest_known_hosts.touch(exist_ok=True)
        if os.name != "nt":
            self.guest_known_hosts.chmod(0o600)


def _ssh_base(key: Path, known_hosts: Path, *, accept_new: bool = False) -> list[str]:
    return [
        "ssh",
        "-F",
        os.devnull,
        "-i",
        os.fspath(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        f"StrictHostKeyChecking={'accept-new' if accept_new else 'yes'}",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
    ]


class SSHPVE:
    def __init__(self, config: SSHConfig):
        self.config = config

    @property
    def base(self) -> list[str]:
        return _ssh_base(self.config.pve_key, self.config.pve_known_hosts)

    def command(self, args: Sequence[str], *, check: bool = True) -> str:
        remote = shlex.join(args)
        result = run(
            [*self.base, self.config.pve_host, remote],
            capture=True,
            check=check,
            input_text="",
        )
        if result.returncode:
            if check:
                raise BuildError(result.stderr.strip())
            return "__FAILED__"
        return result.stdout.strip()

    def stream_command(self, args: Sequence[str]) -> None:
        remote = shlex.join(args)
        run([*self.base, self.config.pve_host, remote], input_text="")


class PVELock:
    """Hold the build-lab flock through one persistent SSH connection."""

    def __init__(self, pve: SSHPVE):
        self.pve = pve
        self.process: subprocess.Popen[str] | None = None

    def acquire(self) -> None:
        remote = (
            f"exec 9>{PVE_LOCK_PATH}; "
            "flock -n 9 || exit 73; "
            "printf 'pid=%s started=%s\\n' \"$$\" \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >&9; "
            "printf 'LOCKED\\n'; cat >/dev/null"
        )
        try:
            process = subprocess.Popen(
                [*self.pve.base, self.pve.config.pve_host, remote],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise BuildError("required command not found: ssh") from exc
        self.process = process
        messages: queue.Queue[str] = queue.Queue(maxsize=1)

        def read_first_line() -> None:
            assert process.stdout is not None
            messages.put(process.stdout.readline())

        threading.Thread(target=read_first_line, daemon=True).start()
        try:
            response = messages.get(timeout=15).strip()
        except queue.Empty:
            response = ""
        if response != "LOCKED":
            self.release()
            raise BuildError("another PVE build/provision operation owns the build-lab lock")
        print("Acquired PVE build-lab lock.", flush=True)

    def release(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        print("Released PVE build-lab lock.", flush=True)

    def __enter__(self) -> "PVELock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class GuestSSH:
    def __init__(self, config: SSHConfig):
        self.config = config

    def _base(self, *, accept_new: bool = False) -> list[str]:
        return _ssh_base(
            self.config.guest_key,
            self.config.guest_known_hosts,
            accept_new=accept_new,
        )

    def command(
        self,
        ip: str,
        remote: str,
        *,
        accept_new: bool = False,
        check: bool = True,
        log: Path | None = None,
    ) -> str:
        args = [*self._base(accept_new=accept_new), f"builder@{ip}", remote]
        if log is None:
            result = run(args, capture=True, check=check, input_text="")
            if result.returncode and not check:
                return "__FAILED__"
            return result.stdout.strip()
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8") as stream:
            result = run(args, stdout=stream, stderr=subprocess.STDOUT, check=False, input_text="")
        if check and result.returncode:
            raise BuildError(f"guest command failed ({result.returncode}): {command_text(args)}")
        return "__FAILED__" if result.returncode else ""

    def powershell(
        self,
        ip: str,
        script: str,
        *,
        check: bool = True,
        log: Path | None = None,
    ) -> str:
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        command = (
            "powershell.exe -NoLogo -NoProfile -NonInteractive "
            f"-ExecutionPolicy Bypass -EncodedCommand {encoded}"
        )
        return self.command(ip, command, check=check, log=log)

    def forget_host(self, ip: str) -> None:
        run(
            ["ssh-keygen", "-f", self.config.guest_known_hosts, "-R", ip],
            capture=True,
            check=False,
        )

    def copy_to(self, ip: str, source: Path, destination: str) -> None:
        run(
            [
                "scp",
                "-O",
                *self._base()[1:],
                source,
                f"builder@{ip}:{destination}",
            ]
        )

    def copy_from(self, ip: str, source: str, destination: Path, *, check: bool = True) -> bool:
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = run(
            [
                "scp",
                "-O",
                *self._base()[1:],
                f"builder@{ip}:{source}",
                destination,
            ],
            check=False,
            capture=not check,
        )
        if check and result.returncode:
            raise BuildError(f"could not retrieve guest file: {source}")
        return result.returncode == 0


def parse_config(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            values[key] = value
    return values


def references_cached_source_media(line: str) -> bool:
    return ":iso/" in line or "/var/lib/vz/template/iso/" in line


class PVEManager:
    def __init__(self, backend: PVECommand, *, protected_vmids: Iterable[int]):
        self.backend = backend
        self.protected_vmids = set(protected_vmids)

    def qm(self, *args: object, check: bool = True) -> str:
        return self.backend.command(["qm", *(str(arg) for arg in args)], check=check)

    def config_text(self, vmid: int) -> str:
        return self.qm("config", vmid)

    def config(self, vmid: int) -> dict[str, str]:
        return parse_config(self.config_text(vmid))

    def status(self, vmid: int) -> str:
        raw = self.qm("status", vmid)
        if not raw.startswith("status: "):
            raise BuildError(f"PVE returned an invalid status for VMID {vmid}: {raw}")
        return raw.removeprefix("status: ")

    def validate_template(self, vmid: int, expected_name: str) -> None:
        config = self.config(vmid)
        if self.status(vmid) != "stopped":
            raise BuildError(f"template {vmid} must be stopped")
        if config.get("name") != expected_name:
            raise BuildError(f"template {vmid} has an unexpected name")
        if config.get("template") != "1":
            raise BuildError(f"VMID {vmid} is not a template")
        if config.get("protection") != "1":
            raise BuildError(f"template {vmid} is not protected")

    def next_vmid(self) -> int:
        raw = self.backend.command(["pvesh", "get", "/cluster/nextid"])
        if not raw.isdecimal():
            raise BuildError(f"PVE returned an invalid VMID: {raw}")
        vmid = int(raw)
        if vmid in self.protected_vmids:
            raise BuildError(f"PVE returned a protected template VMID: {vmid}")
        return vmid

    def wait_for_agent(self, vmid: int, *, attempts: int, interval: float = 2) -> None:
        for _ in range(attempts):
            if self.qm("agent", vmid, "ping", check=False) != "__FAILED__":
                return
            time.sleep(interval)
        raise BuildError(f"QEMU Guest Agent did not become ready for clone {vmid}")

    def guest_ipv4(self, vmid: int) -> str:
        raw = self.qm("agent", vmid, "network-get-interfaces")
        candidates: list[str] = []
        try:
            data = json.loads(raw)
            for interface in data.get("result", data if isinstance(data, list) else []):
                for address in interface.get("ip-addresses", []):
                    candidates.append(address.get("ip-address", ""))
        except (json.JSONDecodeError, AttributeError, TypeError):
            candidates.extend(re.findall(r'"ip-address"\s*:\s*"([0-9.]+)"', raw))
        for address in candidates:
            if IPV4.fullmatch(address) and not address.startswith("127."):
                return address
        raise BuildError(f"no non-loopback IPv4 address reported for clone {vmid}")

    def wait_for_stopped(self, vmid: int, *, attempts: int, interval: float = 2) -> None:
        for _ in range(attempts):
            if self.status(vmid) == "stopped":
                return
            time.sleep(interval)
        raise BuildError(f"clone {vmid} did not stop")

    def detach_cached_source_media(self, vmid: int) -> None:
        for line in self.config_text(vmid).splitlines():
            if not references_cached_source_media(line):
                continue
            slot = line.partition(":")[0]
            if MEDIA_SLOT.fullmatch(slot):
                self.qm("set", vmid, "--delete", slot)
                print(f"Detached cached source media from VMID {vmid} slot {slot}.")
        for line in self.config_text(vmid).splitlines():
            if references_cached_source_media(line):
                raise BuildError(
                    f"refusing to delete VMID {vmid} while its config references "
                    f"cached source media: {line}"
                )

    def remove_owned_clone(
        self,
        vmid: int,
        expected_name: str,
        *,
        force_stop: bool = False,
        shutdown_timeout: int = 120,
    ) -> None:
        if not isinstance(vmid, int) or vmid <= 0:
            raise BuildError(f"invalid disposable VMID: {vmid}")
        if vmid in self.protected_vmids:
            raise BuildError(f"refusing to delete protected template VMID {vmid}")
        config = self.config(vmid)
        if config.get("name") != expected_name:
            raise BuildError(f"clone {vmid} has an unexpected name")
        if config.get("template"):
            raise BuildError(f"refusing to delete template VMID {vmid}")
        if config.get("protection") != "0":
            raise BuildError(f"refusing to delete protected VMID {vmid}")
        status = self.status(vmid)
        if status == "running":
            shutdown = self.qm("shutdown", vmid, "--timeout", shutdown_timeout, check=False)
            if force_stop and shutdown == "__FAILED__":
                self.qm("stop", vmid, "--overrule-shutdown", 1)
        elif status != "stopped":
            raise BuildError(f"clone {vmid} has unsupported status: {status}")
        self.wait_for_stopped(vmid, attempts=90 if force_stop else 60)
        self.detach_cached_source_media(vmid)
        self.qm("destroy", vmid, "--purge", 1)
        print(f"Deleted disposable clone {vmid} ({expected_name}).")

    def list_vms(self) -> list[tuple[int, str]]:
        rows: list[tuple[int, str]] = []
        for line in self.qm("list").splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2 and fields[0].isdecimal():
                rows.append((int(fields[0]), fields[1]))
        return rows
