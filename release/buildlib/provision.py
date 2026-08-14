"""Linux and Windows immutable-template provisioning on a Proxmox VE host."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .core import BuildError, output, require_commands, run, sha256
from .host import HostLock, LocalPVE, guest_powershell, guest_shell, require_root
from .pipelines import (
    DEBIAN_TEMPLATE,
    DEBIAN_TEMPLATE_NAME,
    PROTECTED_VMIDS,
    UBUNTU_TEMPLATE,
    UBUNTU_TEMPLATE_NAME,
    WINDOWS_TEMPLATE,
    WINDOWS_TEMPLATE_NAME,
)
from .pve import PVEManager, parse_config, references_cached_source_media


IMAGE_DIR = Path("/var/lib/vz/template/iso")
SNIPPET_DIR = Path("/var/lib/vz/snippets")


def _validate_host(*, snippets: bool) -> None:
    output(["pveversion"])
    for storage in ("local", "local-lvm"):
        status = output(["pvesm", "status", "--storage", storage])
        if not any(line.split() and line.split()[0] == storage for line in status.splitlines()):
            raise BuildError(f"required storage is unavailable: {storage}")
    run(["ip", "link", "show", "vmbr0"], capture=True)
    if snippets:
        storage_config = Path("/etc/pve/storage.cfg").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^dir: local\n(?P<body>(?:[ \t].*\n)*)", storage_config)
        if not match or "snippets" not in match.group("body"):
            raise BuildError("local storage must allow snippets content")


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, *, resume: bool = False) -> None:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            headers: dict[str, str] = {}
            mode = "wb"
            if resume and destination.exists():
                headers["Range"] = f"bytes={destination.stat().st_size}-"
                mode = "ab"
            request = urllib.request.Request(url, headers=headers)
            response = urllib.request.urlopen(request, timeout=60)
            actual_mode = mode if response.status == 206 else "wb"
            with response, destination.open(actual_mode) as stream:
                shutil.copyfileobj(response, stream, 1024 * 1024)
            return
        except Exception as exc:  # network errors have several concrete urllib types
            last_error = exc
            time.sleep(min(2**attempt, 15))
    raise BuildError(f"download failed: {url}: {last_error}")


def _wait_for_status(manager: PVEManager, vmid: int, wanted: str, attempts: int, interval: int) -> None:
    for _ in range(attempts):
        if manager.status(vmid) == wanted:
            return
        time.sleep(interval)
    raise BuildError(f"VMID {vmid} did not reach {wanted} within the bounded wait")


class LinuxProvisioner:
    def __init__(self, public_key: Path | None, *, check_only: bool):
        self.public_key = public_key
        self.check_only = check_only
        self.backend = LocalPVE()
        self.pve = PVEManager(self.backend, protected_vmids=PROTECTED_VMIDS)
        self.active_vmid: int | None = None

    def thin_pool_values(self) -> tuple[str, str]:
        threshold = output(
            ["lvmconfig", "--type", "full", "activation/thin_pool_autoextend_threshold"]
        ).split("=")[-1].strip()
        percent = output(
            ["lvmconfig", "--type", "full", "activation/thin_pool_autoextend_percent"]
        ).split("=")[-1].strip()
        return threshold, percent

    def configure_thin_pool(self) -> None:
        if self.thin_pool_values() == ("80", "20"):
            return
        config = Path("/etc/lvm/lvm.conf")
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = config.with_name(f"lvm.conf.proxylister.{timestamp}")
        shutil.copy2(config, backup)
        lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
        filtered = [
            line
            for line in lines
            if "thin_pool_autoextend_threshold" not in line
            and "thin_pool_autoextend_percent" not in line
            and "ProxyLister build-lab thin-pool autoextend" not in line
        ]
        inserted = False
        result: list[str] = []
        for line in filtered:
            result.append(line)
            if not inserted and re.match(r"^activation\s*\{\s*$", line):
                result.extend(
                    [
                        "\t# ProxyLister build-lab thin-pool autoextend\n",
                        "\tthin_pool_autoextend_threshold = 80\n",
                        "\tthin_pool_autoextend_percent = 20\n",
                        "\t# End ProxyLister build-lab thin-pool autoextend\n",
                    ]
                )
                inserted = True
        if not inserted:
            raise BuildError("could not update the LVM activation section")
        temporary = config.with_name(f".lvm.conf.proxylister.{os.getpid()}")
        temporary.write_text("".join(result), encoding="utf-8")
        shutil.copystat(config, temporary)
        temporary.replace(config)
        try:
            run(["lvmconfig", "--validate"])
            if self.thin_pool_values() != ("80", "20"):
                raise BuildError("LVM ignored the requested autoextend values")
        except Exception:
            shutil.copy2(backup, config)
            raise
        run(["systemctl", "restart", "lvm2-monitor.service"])
        print(f"Configured LVM thin-pool autoextend; backup: {backup}")

    def validate_template(self, vmid: int, name: str, memory: str) -> None:
        self.pve.validate_template(vmid, name)
        config = self.pve.config(vmid)
        expected = {
            "cores": "2",
            "memory": memory,
            "ciuser": "builder",
            "ipconfig0": "ip=dhcp",
        }
        for key, value in expected.items():
            if config.get(key) != value:
                raise BuildError(f"VMID {vmid} has unexpected {key}")
        text = self.pve.config_text(vmid)
        if "agent: enabled=1" not in text:
            raise BuildError(f"VMID {vmid} must enable QEMU Guest Agent")
        if not re.search(r"^scsi0: local-lvm:base-", text, re.MULTILINE):
            raise BuildError(f"VMID {vmid} must have a base disk on local-lvm")
        print(f"Validated template {vmid} ({name}).")

    def verified_image(
        self, url: str, sums_url: str, algorithm: str, filename: str
    ) -> Path:
        with tempfile.NamedTemporaryFile(prefix="proxylister-sums-", delete=False) as stream:
            sums = Path(stream.name)
        try:
            _download(sums_url, sums)
            expected = None
            for line in sums.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[-1].lstrip("*") == filename:
                    expected = fields[0]
                    break
            if expected is None:
                raise BuildError(f"official checksum list does not contain {filename}")
            target = IMAGE_DIR / filename
            if target.exists():
                if _file_digest(target, algorithm) != expected:
                    raise BuildError(f"existing image failed its official checksum: {target}")
                return target
            temporary = target.with_name(f"{target.name}.download.{os.getpid()}")
            _download(url, temporary)
            if _file_digest(temporary, algorithm) != expected:
                raise BuildError(f"downloaded image failed its official checksum: {temporary}")
            temporary.replace(target)
            return target
        finally:
            sums.unlink(missing_ok=True)

    def template_exists(self, vmid: int) -> bool:
        return Path(f"/etc/pve/qemu-server/{vmid}.conf").exists()

    def validate_linked_clone(self, template: int, verify: str) -> None:
        vmid = self.pve.next_vmid()
        name = f"proxylister-bootstrap-validation-{vmid}"
        self.active_vmid = vmid
        self.pve.qm("clone", template, vmid, "--name", name, "--full", 0)
        self.pve.qm("set", vmid, "--protection", 0)
        self.pve.qm("start", vmid)
        self.pve.wait_for_agent(vmid, attempts=900)
        guest_shell(
            self.backend,
            vmid,
            'cloud-init status --wait >/dev/null 2>&1 || test "$?" -eq 2; '
            'cloud-init status --long | grep -q "^errors: \\[\\]$"',
        )
        guest_shell(self.backend, vmid, verify)
        self.pve.qm("shutdown", vmid, "--timeout", 120)
        _wait_for_status(self.pve, vmid, "stopped", 90, 2)
        self.pve.remove_owned_clone(vmid, name)
        self.active_vmid = None
        print(f"Validated linked-clone boot from template {template}.")

    def create_template(
        self,
        vmid: int,
        name: str,
        memory: str,
        description: str,
        image: Path,
        vendor_text: str,
        verify: str,
    ) -> None:
        if self.template_exists(vmid):
            raise BuildError(f"VMID {vmid} already exists and was not validated")
        if self.public_key is None or not self.public_key.is_file():
            raise BuildError("--ssh-public-key is required to create templates")
        self.active_vmid = vmid
        key_copy = SNIPPET_DIR / f".proxylister-key-{vmid}.pub"
        vendor_path = SNIPPET_DIR / f"proxylister-provision-{vmid}.yaml"
        shutil.copy2(self.public_key, key_copy)
        key_copy.chmod(0o600)
        vendor_path.write_text(vendor_text, encoding="utf-8")
        vendor_path.chmod(0o644)
        self.pve.qm(
            "create", vmid, "--name", name, "--description", description,
            "--ostype", "l26", "--cpu", "host", "--cores", 2,
            "--memory", memory, "--balloon", 0, "--scsihw", "virtio-scsi-single",
            "--net0", "virtio,bridge=vmbr0,firewall=1",
            "--agent", "enabled=1,fstrim_cloned_disks=1",
            "--serial0", "socket", "--vga", "serial0", "--onboot", 0,
        )
        self.pve.qm("importdisk", vmid, image, "local-lvm")
        self.pve.qm(
            "set", vmid,
            "--scsi0", f"local-lvm:vm-{vmid}-disk-0,discard=on,iothread=1,ssd=1",
            "--boot", "order=scsi0", "--ide2", "local-lvm:cloudinit",
            "--ciuser", "builder", "--sshkeys", key_copy, "--ipconfig0", "ip=dhcp",
            "--ciupgrade", 0, "--cicustom", f"vendor=local:snippets/{vendor_path.name}",
        )
        self.pve.qm("resize", vmid, "scsi0", "20G")
        key_copy.unlink(missing_ok=True)
        self.pve.qm("start", vmid)
        self.pve.wait_for_agent(vmid, attempts=900)
        guest_shell(
            self.backend,
            vmid,
            'cloud-init status --wait >/dev/null 2>&1 || test "$?" -eq 2; '
            'cloud-init status --long | grep -q "^errors: \\[\\]$"',
        )
        guest_shell(self.backend, vmid, verify)
        guest_shell(
            self.backend,
            vmid,
            "cloud-init clean --logs --machine-id && rm -f /etc/ssh/ssh_host_* && sync",
        )
        self.pve.qm("shutdown", vmid, "--timeout", 120)
        _wait_for_status(self.pve, vmid, "stopped", 90, 2)
        self.pve.qm("set", vmid, "--delete", "cicustom")
        vendor_path.unlink(missing_ok=True)
        self.pve.qm("template", vmid)
        self.pve.qm("set", vmid, "--protection", 1)
        self.validate_template(vmid, name, memory)
        self.active_vmid = None
        self.validate_linked_clone(vmid, verify)

    def run(self) -> None:
        require_root()
        require_commands(("ip", "lvmconfig", "pvesh", "pvesm", "qm", "qemu-img"))
        _validate_host(snippets=True)
        if self.check_only:
            if self.thin_pool_values() != ("80", "20"):
                raise BuildError("LVM thin-pool autoextend must use threshold 80 and growth 20")
            if not IMAGE_DIR.is_dir() or not SNIPPET_DIR.is_dir():
                raise BuildError("required image or snippet directory is missing")
        else:
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
            self.configure_thin_pool()
        templates = (
            (
                DEBIAN_TEMPLATE,
                DEBIAN_TEMPLATE_NAME,
                "3072",
                "Debian 13 stable immutable builder template",
                "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
                "https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS",
                "sha512",
                "debian-13-genericcloud-amd64.qcow2",
                """#cloud-config
package_update: true
package_upgrade: true
packages: [python3, python3-venv, python3-dev, build-essential, git, openssh-server, ca-certificates, curl, file, qemu-guest-agent]
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent.service]
""",
                "test -f /etc/debian_version && command -v python3 git cc curl qemu-ga >/dev/null",
            ),
            (
                UBUNTU_TEMPLATE,
                UBUNTU_TEMPLATE_NAME,
                "2048",
                "Ubuntu 24.04 LTS immutable binary compatibility-check template",
                "https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img",
                "https://cloud-images.ubuntu.com/releases/noble/release/SHA256SUMS",
                "sha256",
                "ubuntu-24.04-server-cloudimg-amd64.img",
                """#cloud-config
package_update: true
packages: [qemu-guest-agent, ca-certificates]
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent.service]
""",
                'grep -q "^ID=ubuntu$" /etc/os-release && command -v qemu-ga >/dev/null',
            ),
        )
        for vmid, name, memory, description, url, sums, algorithm, filename, vendor, verify in templates:
            if self.template_exists(vmid):
                self.validate_template(vmid, name, memory)
            elif self.check_only:
                raise BuildError(f"required template is missing: {vmid}")
            else:
                image = self.verified_image(url, sums, algorithm, filename)
                self.create_template(vmid, name, memory, description, image, vendor, verify)
        print("PVE Linux build-lab provisioning is complete.")


WINDOWS_DESCRIPTION = (
    "Windows 11 Enterprise Evaluation 25H2 immutable ProxyLister ready-state-v1 "
    "builder template; official pinned media; no cumulative update pass"
)
WINDOWS_MEMORY = "4096"
WINDOWS_MEDIA = (
    (
        "windows-11-enterprise-eval-25h2-en-us.iso",
        "https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/26200.6584.250915-1905.25h2_ge_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso",
        "a61adeab895ef5a4db436e0a7011c92a2ff17bb0357f58b13bbc4062e535e7b9",
    ),
    (
        "virtio-win-0.1.285.iso",
        "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.285-1/virtio-win-0.1.285.iso",
        "e14cf2b94492c3e925f0070ba7fdfedeb2048c91eea9c5a5afb30232a3976331",
    ),
    (
        "python-3.13.15-amd64.exe",
        "https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe",
        "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403",
    ),
    (
        "OpenSSH-Win64-v10.0.0.0.msi",
        "https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/OpenSSH-Win64-v10.0.0.0.msi",
        "ddec9c53864280759cf9f74791cefd387100e3946aa849a1c138a4ed1b96b7d9",
    ),
)


class WindowsProvisioner:
    def __init__(self, assets: Path, public_key: Path | None, *, check_only: bool):
        self.assets = assets
        self.public_key = public_key
        self.check_only = check_only
        self.backend = LocalPVE()
        self.pve = PVEManager(self.backend, protected_vmids=PROTECTED_VMIDS)
        self.active_vmid: int | None = None

    def exists(self, vmid: int) -> bool:
        return Path(f"/etc/pve/qemu-server/{vmid}.conf").exists()

    def verify_cached(self, filename: str, expected: str) -> Path:
        target = IMAGE_DIR / filename
        if not target.is_file():
            raise BuildError(f"required cached official artifact is missing: {target}")
        if sha256(target) != expected:
            raise BuildError(f"cached official artifact failed its pinned SHA256: {target}")
        print(f"Verified cached source: {target}")
        return target

    def verified_download(self, filename: str, url: str, expected: str) -> None:
        target = IMAGE_DIR / filename
        if target.exists():
            self.verify_cached(filename, expected)
            return
        partial = target.with_suffix(target.suffix + ".part")
        if not partial.is_file() or sha256(partial) != expected:
            _download(url, partial, resume=True)
        if sha256(partial) != expected:
            raise BuildError(f"official download failed pinned SHA256: {partial}")
        partial.replace(target)
        print(f"Downloaded and verified official source: {target}")

    def validate_template(self, require_protection: bool = True) -> None:
        config = self.pve.config(WINDOWS_TEMPLATE)
        if self.pve.status(WINDOWS_TEMPLATE) != "stopped":
            raise BuildError("Windows template must be stopped")
        expected = {
            "name": WINDOWS_TEMPLATE_NAME,
            "template": "1",
            "cores": "2",
            "memory": WINDOWS_MEMORY,
            "bios": "ovmf",
        }
        for key, value in expected.items():
            if config.get(key) != value:
                raise BuildError(f"Windows template has unexpected {key}")
        if config.get("description") != WINDOWS_DESCRIPTION:
            raise BuildError("Windows template has unexpected description")
        if require_protection and config.get("protection") != "1":
            raise BuildError("Windows template is not protected")
        if config.get("machine") != "q35" and not config.get("machine", "").startswith("pc-q35-"):
            raise BuildError("Windows template must use q35")
        text = self.pve.config_text(WINDOWS_TEMPLATE)
        for required in (
            "agent: enabled=1",
            "efidisk0: local-lvm:base-",
            "tpmstate0: local-lvm:base-",
            "sata0: local-lvm:base-",
        ):
            if required not in text:
                raise BuildError(f"Windows template is missing required config: {required}")
        for line in text.splitlines():
            if references_cached_source_media(line):
                raise BuildError(f"Windows template still references source media: {line}")

    def stop(self, vmid: int) -> None:
        status = self.pve.status(vmid)
        if status == "running":
            if self.pve.qm("shutdown", vmid, "--timeout", 180, check=False) == "__FAILED__":
                self.pve.qm("stop", vmid, "--overrule-shutdown", 1)
            _wait_for_status(self.pve, vmid, "stopped", 60, 5)
        elif status != "stopped":
            raise BuildError(f"VMID {vmid} has unsupported status: {status}")

    def purge_candidate(self, vmid: int, expected_name: str, *, allow_template: bool = False) -> None:
        if vmid != WINDOWS_TEMPLATE and not expected_name.startswith("proxylister-windows-validation-"):
            raise BuildError(f"refusing to purge unexpected VMID {vmid}")
        config = self.pve.config(vmid)
        if config.get("name") != expected_name:
            raise BuildError(f"VMID {vmid} has an unexpected name")
        if config.get("protection") != "0":
            raise BuildError(f"refusing to purge protected VMID {vmid}")
        if config.get("template") == "1" and not allow_template:
            raise BuildError(f"refusing to purge template VMID {vmid}")
        self.stop(vmid)
        self.pve.detach_cached_source_media(vmid)
        self.pve.qm("destroy", vmid, "--purge", 1)

    def wait_ready_shutdown(self, vmid: int, attempts: int) -> None:
        ready = False
        for _ in range(attempts):
            status = self.pve.status(vmid)
            if status == "stopped":
                if not ready:
                    raise BuildError(f"VMID {vmid} stopped before the template ready marker was observed")
                return
            if status != "running":
                raise BuildError(f"VMID {vmid} has unsupported provisioning status: {status}")
            if self.pve.qm("agent", vmid, "ping", check=False) != "__FAILED__":
                ready = guest_powershell(
                    self.backend,
                    vmid,
                    r"if (-not (Test-Path C:\proxylister-template-ready.json)) { exit 1 }",
                    check=False,
                ) or ready
            time.sleep(5)
        raise BuildError(f"VMID {vmid} did not report readiness and stop within the bounded wait")

    def validate_linked_clone(self) -> None:
        vmid = self.pve.next_vmid()
        name = f"proxylister-windows-validation-{vmid}"
        self.active_vmid = vmid
        self.pve.qm("clone", WINDOWS_TEMPLATE, vmid, "--name", name, "--full", 0)
        self.pve.qm("set", vmid, "--protection", 0)
        self.pve.qm("start", vmid)
        self.pve.wait_for_agent(vmid, attempts=360, interval=5)
        guest_powershell(
            self.backend,
            vmid,
            r"""
$ErrorActionPreference='Stop'
if (-not (Test-Path C:\proxylister-template-ready.json)) { throw 'ready marker missing' }
$ready=Get-Content C:\proxylister-template-ready.json -Raw | ConvertFrom-Json
if ($ready.template_mode -ne 'ready-state-v1') { throw 'unexpected template mode' }
if ((Get-Service QEMU-GA).Status -ne 'Running') { throw 'QGA stopped' }
if ((Get-Service sshd).Status -ne 'Running') { throw 'sshd stopped' }
if (-not (Test-NetConnection 127.0.0.1 -Port 22 -InformationLevel Quiet)) { throw 'sshd not listening' }
if (-not (Test-Path C:\ProgramData\ssh\ssh_host_ed25519_key)) { throw 'SSH host key missing' }
$v=& C:\Python313\python.exe --version 2>&1
if ($v -notmatch '^Python 3\.13\.') { throw "bad Python: $v" }
$os=Get-CimInstance Win32_OperatingSystem
if ($os.Caption -notmatch 'Windows 11 Enterprise Evaluation') { throw "bad OS: $($os.Caption)" }
""",
        )
        self.pve.qm("shutdown", vmid, "--timeout", 180)
        _wait_for_status(self.pve, vmid, "stopped", 60, 5)
        self.purge_candidate(vmid, name)
        self.active_vmid = None

    def build_answer_iso(self, password: str) -> Path:
        answer_iso = IMAGE_DIR / "proxylister-windows-unattend-9002.iso"
        with tempfile.TemporaryDirectory(prefix="proxylister-windows-answer-") as temporary:
            answer = Path(temporary)
            for name in ("autounattend.xml", "bootstrap.ps1"):
                source = self.assets / name
                if not source.is_file():
                    raise BuildError(f"required provisioning source is missing: {source}")
                shutil.copy2(source, answer / name)
            xml = (answer / "autounattend.xml").read_text(encoding="utf-8")
            (answer / "autounattend.xml").write_text(
                xml.replace("@@PASSWORD@@", password), encoding="utf-8"
            )
            assert self.public_key is not None
            shutil.copy2(self.public_key, answer / "builder.pub")
            shutil.copy2(IMAGE_DIR / WINDOWS_MEDIA[2][0], answer / WINDOWS_MEDIA[2][0])
            shutil.copy2(IMAGE_DIR / WINDOWS_MEDIA[3][0], answer / WINDOWS_MEDIA[3][0])
            answer_iso.unlink(missing_ok=True)
            run(["genisoimage", "-quiet", "-J", "-r", "-V", "PROXYLISTER", "-o", answer_iso, answer])
        return answer_iso

    def create_candidate(self) -> None:
        self.active_vmid = WINDOWS_TEMPLATE
        self.pve.qm(
            "create", WINDOWS_TEMPLATE, "--name", WINDOWS_TEMPLATE_NAME,
            "--description", "Unverified Windows 11 build-template candidate",
            "--ostype", "win11", "--machine", "q35", "--bios", "ovmf",
            "--cpu", "host", "--cores", 2, "--memory", WINDOWS_MEMORY,
            "--balloon", 0, "--agent", "enabled=1",
            "--net0", "e1000,bridge=vmbr0,firewall=1", "--vga", "std", "--onboot", 0,
        )
        self.pve.qm(
            "set", WINDOWS_TEMPLATE,
            "--efidisk0", "local-lvm:1,efitype=4m,pre-enrolled-keys=1",
            "--tpmstate0", "local-lvm:4,version=v2.0",
            "--sata0", "local-lvm:48,discard=on,ssd=1",
            "--ide0", f"local:iso/{WINDOWS_MEDIA[0][0]},media=cdrom",
            "--ide2", f"local:iso/{WINDOWS_MEDIA[1][0]},media=cdrom",
            "--sata1", "local:iso/proxylister-windows-unattend-9002.iso,media=cdrom",
        )
        self.pve.qm("set", WINDOWS_TEMPLATE, "--boot", "order=ide0;sata0")
        self.pve.qm("set", WINDOWS_TEMPLATE, "--protection", 0)
        self.pve.qm("start", WINDOWS_TEMPLATE)
        for _ in range(8):
            time.sleep(1)
            self.pve.qm("sendkey", WINDOWS_TEMPLATE, "ret")
        self.wait_ready_shutdown(WINDOWS_TEMPLATE, 720)
        self.pve.detach_cached_source_media(WINDOWS_TEMPLATE)
        self.pve.qm("set", WINDOWS_TEMPLATE, "--boot", "order=sata0")
        (IMAGE_DIR / "proxylister-windows-unattend-9002.iso").unlink(missing_ok=True)
        self.pve.qm("template", WINDOWS_TEMPLATE)
        self.pve.qm("set", WINDOWS_TEMPLATE, "--description", WINDOWS_DESCRIPTION)
        self.validate_template(False)
        self.validate_linked_clone()
        self.pve.qm(
            "set", WINDOWS_TEMPLATE, "--description", WINDOWS_DESCRIPTION, "--protection", 1
        )
        self.validate_template(True)
        self.active_vmid = None

    def run(self) -> None:
        require_root()
        require_commands(("genisoimage", "ip", "pvesh", "pvesm", "qm"))
        _validate_host(snippets=False)
        if not IMAGE_DIR.is_dir():
            raise BuildError(f"media cache is missing: {IMAGE_DIR}")
        if self.check_only:
            for filename, _, digest in WINDOWS_MEDIA:
                self.verify_cached(filename, digest)
            if not self.exists(WINDOWS_TEMPLATE):
                raise BuildError(f"Windows template is missing: {WINDOWS_TEMPLATE}")
            self.validate_template(True)
            return
        if self.public_key is None or not self.public_key.is_file():
            raise BuildError("--ssh-public-key must name the dedicated public key")
        for filename, url, digest in WINDOWS_MEDIA:
            self.verified_download(filename, url, digest)
        if self.exists(WINDOWS_TEMPLATE):
            config = self.pve.config(WINDOWS_TEMPLATE)
            if config.get("template") == "1":
                if config.get("protection") == "1":
                    self.validate_template(True)
                    return
                self.active_vmid = WINDOWS_TEMPLATE
                self.pve.qm("set", WINDOWS_TEMPLATE, "--description", WINDOWS_DESCRIPTION)
                self.validate_template(False)
                self.validate_linked_clone()
                self.pve.qm(
                    "set", WINDOWS_TEMPLATE,
                    "--description", WINDOWS_DESCRIPTION, "--protection", 1,
                )
                self.validate_template(True)
                self.active_vmid = None
                return
            if config.get("name") != WINDOWS_TEMPLATE_NAME:
                raise BuildError(f"VMID {WINDOWS_TEMPLATE} is occupied by an unrelated guest")
            self.purge_candidate(WINDOWS_TEMPLATE, WINDOWS_TEMPLATE_NAME, allow_template=True)
        self.build_answer_iso(secrets.token_hex(16))
        self.create_candidate()
        print("Windows template provisioning completed.")


def provision_host(target: str, assets: Path, public_key: Path | None, check_only: bool) -> None:
    provisioner: LinuxProvisioner | WindowsProvisioner
    if target == "linux":
        provisioner = LinuxProvisioner(public_key, check_only=check_only)
    elif target == "windows":
        provisioner = WindowsProvisioner(assets, public_key, check_only=check_only)
    else:
        raise BuildError(f"unsupported provisioning target: {target}")
    if check_only:
        provisioner.run()
    else:
        with HostLock():
            try:
                provisioner.run()
            except Exception:
                active = provisioner.active_vmid
                if active is not None:
                    print(
                        f"provision: failed VMID {active} was left intact for diagnosis",
                        file=os.sys.stderr,
                    )
                raise
