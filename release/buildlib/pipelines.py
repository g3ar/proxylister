"""Local and PVE native build pipelines built on the shared lifecycle primitives."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .core import BuildError, promote_directory, remove_tree, tail, verify_checksums
from .native import build_native
from .pve import GuestSSH, PVEManager, SSHPVE, SSHConfig
from .source import SourceSnapshot


DEBIAN_TEMPLATE = 9000
DEBIAN_TEMPLATE_NAME = "proxytools-linux-template"
UBUNTU_TEMPLATE = 9001
UBUNTU_TEMPLATE_NAME = "proxytools-ubuntu-2404-check-template"
WINDOWS_TEMPLATE = 9002
WINDOWS_TEMPLATE_NAME = "proxytools-windows-template"
PROTECTED_VMIDS = {DEBIAN_TEMPLATE, UBUNTU_TEMPLATE, WINDOWS_TEMPLATE}


@dataclass
class Clone:
    vmid: int
    name: str
    ip: str
    stage: str


class PVEBuildContext:
    def __init__(self, root: Path, platform_name: str, snapshot: SourceSnapshot):
        self.root = root
        self.platform_name = platform_name
        self.work = root / "release/.work" / f"pve-{platform_name}"
        self.bin = root / "release/bin" / platform_name
        self.artifacts = self.work / "artifacts"
        self.logs = self.work / "logs"
        self.snapshot = snapshot
        self.ssh_config = SSHConfig.from_environment(self.work)
        self.ssh_config.validate()
        self.backend = SSHPVE(self.ssh_config)
        self.pve = PVEManager(self.backend, protected_vmids=PROTECTED_VMIDS)
        self.guest = GuestSSH(self.ssh_config)
        self.active: Clone | None = None

    def prepare(self, stages: tuple[str, ...]) -> None:
        remove_tree(self.work)
        remove_tree(self.bin)
        self.artifacts.mkdir(parents=True)
        for stage in stages:
            (self.logs / stage).mkdir(parents=True, exist_ok=True)
        self.ssh_config.validate()

    def wait_for_ssh(self, ip: str, *, windows: bool) -> None:
        remote = "cmd.exe /d /c exit 0" if windows else "true"
        attempts = 180 if windows else 90
        for _ in range(attempts):
            if self.guest.command(ip, remote, accept_new=True, check=False) != "__FAILED__":
                return
            time.sleep(2)
        raise BuildError(f"SSH did not become ready at {ip}")

    def create_clone(
        self,
        template: int,
        prefix: str,
        stage: str,
        *,
        windows: bool = False,
    ) -> Clone:
        vmid = self.pve.next_vmid()
        clone = Clone(vmid, f"{prefix}-{vmid}", "", stage)
        self.active = clone
        self.pve.qm("clone", template, vmid, "--name", clone.name, "--full", 0)
        self.pve.qm("set", vmid, "--protection", 0)
        self.pve.qm("start", vmid)
        self.pve.wait_for_agent(vmid, attempts=360 if windows else 180)
        clone.ip = self.pve.guest_ipv4(vmid)
        self.guest.forget_host(clone.ip)
        self.wait_for_ssh(clone.ip, windows=windows)
        if windows:
            self._validate_windows_guest(clone.ip)
            self._activate_windows_guest(clone.ip)
        else:
            cloud_log = self.logs / stage / "cloud-init.log"
            command = (
                "cloud-init status --wait; status=$?; cloud-init status --long; "
                "test $status -eq 0 -o $status -eq 2"
            )
            self.guest.command(clone.ip, command, log=cloud_log)
            if "errors: []" not in cloud_log.read_text(encoding="utf-8", errors="replace"):
                raise BuildError(f"cloud-init reported errors in clone {vmid}")
        print(f"PVE clone {clone.vmid} ({clone.name}) is ready at {clone.ip}.")
        return clone

    def _validate_windows_guest(self, ip: str) -> None:
        self.guest.powershell(
            ip,
            r"""
$ErrorActionPreference='Stop'
$marker=@('C:\proxylister-template-ready.json','C:\proxytools-template-ready.json') |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $marker) { throw 'ready marker missing' }
$ready=Get-Content $marker -Raw | ConvertFrom-Json
if ($ready.template_mode -ne 'ready-state-v1') { throw 'unexpected template mode' }
if ((Get-Service QEMU-GA).Status -ne 'Running') { throw 'QGA stopped' }
if ((Get-Service sshd).Status -ne 'Running') { throw 'sshd stopped' }
$v=& C:\Python313\python.exe --version 2>&1
if ($v -notmatch '^Python 3\.13\.') { throw 'unexpected Python' }
""",
        )

    def _activate_windows_guest(self, ip: str) -> None:
        self.guest.powershell(
            ip,
            r"""
$ErrorActionPreference='Stop'
function Get-EvaluationLicense {
    Get-CimInstance SoftwareLicensingProduct |
        Where-Object { $_.PartialProductKey -and $_.Description -match 'TIMEBASED_EVAL' } |
        Select-Object -First 1
}
$license=Get-EvaluationLicense
if (-not $license) { throw 'Windows Evaluation license not found' }
if ($license.LicenseStatus -ne 1 -or $license.GracePeriodRemaining -le 0) {
    $output=& cscript.exe //Nologo C:\Windows\System32\slmgr.vbs /ato 2>&1
    if ($LASTEXITCODE -ne 0) { throw ('Windows Evaluation activation failed: {0}' -f ($output -join ' ')) }
    Start-Sleep -Seconds 5
    $license=Get-EvaluationLicense
}
if ($license.LicenseStatus -ne 1 -or $license.GracePeriodRemaining -le 0) {
    throw ('Windows Evaluation is not licensed: status={0} grace={1}' -f $license.LicenseStatus, $license.GracePeriodRemaining)
}
Write-Output ('Windows Evaluation licensed; grace minutes: {0}' -f $license.GracePeriodRemaining)
""",
        )

    def cleanup_active(self, *, windows: bool = False) -> None:
        if self.active is None:
            raise BuildError("no active clone to clean up")
        self.pve.remove_owned_clone(
            self.active.vmid,
            self.active.name,
            force_stop=windows,
            shutdown_timeout=180 if windows else 120,
        )
        self.active = None

    def report_failure(self) -> None:
        if self.active is None:
            return
        self.retrieve_logs(self.logs / f"failed-{self.active.vmid}", optional=True)
        print(
            f"pve-{self.platform_name}-build: failed clone retained: "
            f"VMID={self.active.vmid} name={self.active.name} IP={self.active.ip or 'unknown'}",
            file=os.sys.stderr,
        )
        print(f"Diagnostics retained in {self.work}", file=os.sys.stderr)

    def cleanup_stale(self, prefixes: tuple[str, ...], *, windows: bool = False) -> None:
        for vmid, name in self.pve.list_vms():
            exact = next((prefix for prefix in prefixes if name == f"{prefix}-{vmid}"), None)
            if exact:
                print(f"Removing stale clone from a previous failed run: {vmid} ({name}).")
                self.pve.remove_owned_clone(vmid, name, force_stop=windows)
            elif any(name.startswith(f"{prefix}-") for prefix in prefixes):
                raise BuildError(f"owned-looking VM {vmid} has an unexpected name: {name}")

    def transfer_source(self, clone: Clone, *, windows: bool) -> None:
        if windows:
            self.guest.powershell(
                clone.ip,
                r"""
$root='C:\Users\builder\proxylister-build'
Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path ($root+'\source') -Force | Out-Null
""",
            )
            self.guest.copy_to(clone.ip, self.snapshot.archive, "proxylister-build/source.tar")
            self.guest.copy_to(clone.ip, self.snapshot.checksum, "proxylister-build/source.tar.sha256")
            self.guest.powershell(
                clone.ip,
                r"""
$ErrorActionPreference='Stop'
$root='C:\Users\builder\proxylister-build'
$expected=((Get-Content -LiteralPath ($root+'\source.tar.sha256')) -split '\s+')[0]
$actual=(Get-FileHash -LiteralPath ($root+'\source.tar') -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw 'source archive checksum mismatch' }
tar.exe -xf ($root+'\source.tar') -C ($root+'\source')
if ($LASTEXITCODE -ne 0) { throw 'source extraction failed' }
""",
            )
            return
        self.guest.command(
            clone.ip,
            "rm -rf /home/builder/proxylister && mkdir -p /home/builder/proxylister",
        )
        self.guest.copy_to(clone.ip, self.snapshot.archive, "/home/builder/source.tar")
        self.guest.copy_to(clone.ip, self.snapshot.checksum, "/home/builder/source.tar.sha256")
        script = (
            "from pathlib import Path; import hashlib,tarfile; "
            "a=Path('/home/builder/source.tar'); "
            "e=Path('/home/builder/source.tar.sha256').read_text().split()[0]; "
            "assert hashlib.sha256(a.read_bytes()).hexdigest()==e; "
            "tarfile.open(a).extractall('/home/builder/proxylister')"
        )
        self.guest.command(clone.ip, f"python3 -c {shlex_quote(script)}")

    def retrieve_logs(self, destination: Path, *, optional: bool) -> None:
        if self.active is None or not self.active.ip:
            return
        destination.mkdir(parents=True, exist_ok=True)
        work_name = "windows" if self.platform_name == "windows" else "local-linux"
        base = (
            "proxylister-build/source/release/.work/windows/logs"
            if self.platform_name == "windows"
            else "/home/builder/proxylister/release/.work/local-linux/logs"
        )
        for name in (
            "build.log",
            "smoke.log",
            "live-list.log",
            "live-list-error.log",
            "live-monitor.log",
        ):
            self.guest.copy_from(
                self.active.ip,
                f"{base}/{name}",
                destination / name,
                check=not optional and name in {"build.log", "smoke.log"},
            )


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def local_linux_build(root: Path, snapshot: SourceSnapshot) -> Path:
    if not sys.platform.startswith("linux"):
        raise BuildError("a local Linux build requires Linux; use --pve on another control OS")
    return build_native(root, "linux", snapshot.commit, snapshot.tree)


def _retrieve_artifacts(
    context: PVEBuildContext,
    clone: Clone,
    platform_name: str,
) -> None:
    names = [
        "proxylister.exe" if platform_name == "windows" else "proxylister",
        "README.md",
        "LICENSE",
        "MANIFEST.txt",
        "SHA256SUMS",
    ]
    base = (
        "proxylister-build/source/release/bin/windows"
        if platform_name == "windows"
        else "/home/builder/proxylister/release/bin/linux"
    )
    for name in names:
        context.guest.copy_from(clone.ip, f"{base}/{name}", context.artifacts / name)
    verify_checksums(context.artifacts)


def pve_linux_build(root: Path, snapshot: SourceSnapshot) -> Path:
    context = PVEBuildContext(root, "linux", snapshot)
    context.prepare(("debian", "ubuntu"))
    context.pve.validate_template(DEBIAN_TEMPLATE, DEBIAN_TEMPLATE_NAME)
    context.pve.validate_template(UBUNTU_TEMPLATE, UBUNTU_TEMPLATE_NAME)
    context.cleanup_stale((
        "proxylister-debian-build",
        "proxylister-ubuntu-validation",
        "proxytools-debian-build",
        "proxytools-ubuntu-validation",
    ))
    try:
        debian = context.create_clone(
            DEBIAN_TEMPLATE, "proxylister-debian-build", "debian"
        )
        context.transfer_source(debian, windows=False)
        build_log = context.logs / "debian/driver-build.log"
        command = (
            "cd /home/builder/proxylister && "
            f"python3 release/build.py _native linux --source-commit {snapshot.commit} "
            f"--source-tree {snapshot.tree}"
        )
        try:
            context.guest.command(debian.ip, command, log=build_log)
        except BuildError as exc:
            raise BuildError(f"Debian build gate failed:\n{tail(build_log)}") from exc
        live_log = context.logs / "debian/driver-live.log"
        try:
            context.guest.command(
                debian.ip,
                "cd /home/builder/proxylister && python3 release/smoke.py live release/bin/linux/proxylister",
                log=live_log,
            )
        except BuildError as exc:
            raise BuildError(f"Debian live-smoke gate failed:\n{tail(live_log)}") from exc
        _retrieve_artifacts(context, debian, "linux")
        context.retrieve_logs(context.logs / "debian", optional=False)
        context.cleanup_active()

        ubuntu = context.create_clone(
            UBUNTU_TEMPLATE, "proxylister-ubuntu-validation", "ubuntu"
        )
        context.transfer_source(ubuntu, windows=False)
        context.guest.command(ubuntu.ip, "mkdir -p /home/builder/proxylister/release/bin/linux")
        for path in context.artifacts.iterdir():
            context.guest.copy_to(
                ubuntu.ip, path, f"/home/builder/proxylister/release/bin/linux/{path.name}"
            )
        validation_log = context.logs / "ubuntu/driver-validation.log"
        validation = (
            "cd /home/builder/proxylister && "
            "python3 -c \"import sys;sys.path.insert(0,'release');"
            "from buildlib.core import verify_checksums;from pathlib import Path;"
            "verify_checksums(Path('release/bin/linux'))\" && "
            "python3 release/smoke.py offline release/bin/linux/proxylister && "
            "python3 release/smoke.py live release/bin/linux/proxylister"
        )
        try:
            context.guest.command(ubuntu.ip, validation, log=validation_log)
        except BuildError as exc:
            raise BuildError(f"Ubuntu compatibility gate failed:\n{tail(validation_log)}") from exc
        context.retrieve_logs(context.logs / "ubuntu", optional=True)
        context.cleanup_active()
        promote_directory(context.artifacts, context.bin)
        return context.bin / "proxylister"
    except Exception:
        context.report_failure()
        raise


def pve_windows_build(root: Path, snapshot: SourceSnapshot) -> Path:
    context = PVEBuildContext(root, "windows", snapshot)
    context.prepare(("guest",))
    context.pve.validate_template(WINDOWS_TEMPLATE, WINDOWS_TEMPLATE_NAME)
    context.cleanup_stale(
        ("proxylister-windows-build", "proxytools-windows-build"),
        windows=True,
    )
    try:
        clone = context.create_clone(
            WINDOWS_TEMPLATE,
            "proxylister-windows-build",
            "windows",
            windows=True,
        )
        context.transfer_source(clone, windows=True)
        build_log = context.logs / "driver-build.log"
        command = (
            r"cd /d C:\Users\builder\proxylister-build\source && "
            r"C:\Python313\python.exe release\build.py _native windows "
            f"--source-commit {snapshot.commit} --source-tree {snapshot.tree}"
        )
        try:
            context.guest.command(clone.ip, f"cmd.exe /d /c \"{command}\"", log=build_log)
        except BuildError as exc:
            raise BuildError(f"Windows build gate failed:\n{tail(build_log)}") from exc
        live_log = context.logs / "driver-live.log"
        live = (
            r"cd /d C:\Users\builder\proxylister-build\source && "
            r"C:\Python313\python.exe release\smoke.py live "
            r"release\bin\windows\proxylister.exe"
        )
        try:
            context.guest.command(clone.ip, f"cmd.exe /d /c \"{live}\"", log=live_log)
        except BuildError as exc:
            raise BuildError(f"Windows live-smoke gate failed:\n{tail(live_log)}") from exc
        _retrieve_artifacts(context, clone, "windows")
        context.retrieve_logs(context.logs / "guest", optional=False)
        context.cleanup_active(windows=True)
        promote_directory(context.artifacts, context.bin)
        return context.bin / "proxylister.exe"
    except Exception:
        context.report_failure()
        raise
