"""Offline regression tests for the shared Python build and PVE safety layer."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

RELEASE = Path(__file__).resolve().parents[1]
ROOT = RELEASE.parent
sys.path.insert(0, os.fspath(RELEASE))

from buildlib.core import BuildError, promote_directory, verify_checksums, write_checksums
from buildlib.pve import PVEManager, SSHConfig, configured_pve_destination
from buildlib import publish
from buildlib.publish import create_release_packages, validate_release_artifacts
from buildlib.provision import WINDOWS_MEDIA, WindowsProvisioner
from buildlib.source import create_snapshot, verify_snapshot
from smoke import _assert_matching_proxy_output


class FakePVE:
    def __init__(self) -> None:
        self.vms: dict[int, dict[str, object]] = {}
        self.commands: list[tuple[str, ...]] = []
        self.status_sequences: dict[int, list[str]] = {}

    def add(
        self,
        vmid: int,
        name: str,
        *,
        template: str = "",
        protection: str = "0",
        status: str = "stopped",
        extra: tuple[str, ...] = (),
    ) -> None:
        self.vms[vmid] = {
            "name": name,
            "template": template,
            "protection": protection,
            "status": status,
            "extra": list(extra),
        }

    def command(self, args: list[str] | tuple[str, ...], *, check: bool = True) -> str:
        command = tuple(args)
        self.commands.append(command)
        if command[:2] == ("qm", "list"):
            rows = [" VMID NAME STATUS"]
            rows.extend(
                f" {vmid} {vm['name']} {vm['status']}" for vmid, vm in self.vms.items()
            )
            return "\n".join(rows)
        if command[:2] == ("qm", "config"):
            vm = self.vms[int(command[2])]
            lines = [f"name: {vm['name']}", f"protection: {vm['protection']}"]
            if vm["template"]:
                lines.append(f"template: {vm['template']}")
            lines.extend(vm["extra"])  # type: ignore[arg-type]
            return "\n".join(lines)
        if command[:2] == ("qm", "status"):
            vmid = int(command[2])
            sequence = self.status_sequences.get(vmid, [])
            if sequence:
                self.vms[vmid]["status"] = sequence.pop(0)
            return f"status: {self.vms[vmid]['status']}"
        if command[:2] == ("qm", "agent"):
            return ""
        if command[:2] in (("qm", "shutdown"), ("qm", "stop")):
            self.vms[int(command[2])]["status"] = "stopped"
            return ""
        if command[:2] == ("qm", "set") and "--delete" in command:
            vmid = int(command[2])
            slot = command[command.index("--delete") + 1]
            self.vms[vmid]["extra"] = [
                line
                for line in self.vms[vmid]["extra"]  # type: ignore[union-attr]
                if not str(line).startswith(f"{slot}:")
            ]
            return ""
        if command[:2] == ("qm", "destroy"):
            return ""
        raise AssertionError(f"unexpected fake PVE command: {command}")


class SSHConfigTests(unittest.TestCase):
    def test_pve_host_comes_from_python_config_and_always_uses_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "buildlib.pve.PVE_HOST", "pve-build.example"
        ), patch.dict(
            os.environ,
            {"PROXYLISTER_PVE_HOST": "ignored.example"},
            clear=True,
        ):
            config = SSHConfig.from_environment(Path(temporary))
        self.assertEqual(config.pve_host, "root@pve-build.example")

    def test_pve_host_rejects_a_user_or_whitespace(self) -> None:
        for value in ("", "root@pve-build.example", "pve build"):
            with self.subTest(value=value), patch("buildlib.pve.PVE_HOST", value):
                with self.assertRaisesRegex(BuildError, "hostname or IP address"):
                    configured_pve_destination()


class LiveSmokeTests(unittest.TestCase):
    def test_saved_proxy_file_must_exactly_match_live_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout = root / "stdout.log"
            saved = root / "working_proxies.txt"
            expected = "http://192.0.2.1:80\nsocks5://192.0.2.2:1080\n"
            stdout.write_text(expected, encoding="utf-8")
            saved.write_text(expected, encoding="utf-8")
            _assert_matching_proxy_output(stdout, saved, 2)

            saved.write_text("http://192.0.2.1:80\n", encoding="utf-8")
            with self.assertRaisesRegex(BuildError, "does not match"):
                _assert_matching_proxy_output(stdout, saved, 2)

    def test_live_output_requires_multiple_valid_proxies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout = root / "stdout.log"
            saved = root / "working_proxies.txt"
            stdout.write_text("http://192.0.2.1:80\n", encoding="utf-8")
            saved.write_text("http://192.0.2.1:80\n", encoding="utf-8")
            with self.assertRaisesRegex(BuildError, "expected at least 2"):
                _assert_matching_proxy_output(stdout, saved, 2)


class PVESafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakePVE()
        self.manager = PVEManager(self.backend, protected_vmids={9000, 9001, 9002})

    def test_owned_clone_is_stopped_detached_and_destroyed(self) -> None:
        self.backend.add(
            101,
            "proxylister-debian-build-101",
            status="running",
            extra=(
                "ide0: local:iso/debian.iso,media=cdrom",
                "sata1: /var/lib/vz/template/iso/virtio.iso,media=cdrom",
                "ide2: local-lvm:cloudinit,media=cdrom",
            ),
        )
        self.manager.remove_owned_clone(101, "proxylister-debian-build-101")
        self.assertIn(("qm", "shutdown", "101", "--timeout", "120"), self.backend.commands)
        self.assertIn(("qm", "set", "101", "--delete", "ide0"), self.backend.commands)
        self.assertIn(("qm", "set", "101", "--delete", "sata1"), self.backend.commands)
        self.assertIn(("qm", "destroy", "101", "--purge", "1"), self.backend.commands)
        self.assertEqual(self.backend.vms[101]["extra"], ["ide2: local-lvm:cloudinit,media=cdrom"])

    def test_wrong_name_template_protection_and_base_vmid_are_rejected(self) -> None:
        cases = (
            (102, "proxylister-debian-build-999", "", "0", "unexpected name"),
            (103, "proxylister-debian-build-103", "1", "0", "delete template"),
            (104, "proxylister-debian-build-104", "", "1", "delete protected"),
        )
        for vmid, name, template, protection, message in cases:
            with self.subTest(vmid=vmid):
                self.backend.add(vmid, name, template=template, protection=protection)
                with self.assertRaisesRegex(BuildError, message):
                    self.manager.remove_owned_clone(vmid, f"proxylister-debian-build-{vmid}")
        with self.assertRaisesRegex(BuildError, "protected template VMID"):
            self.manager.remove_owned_clone(9000, "proxylister-linux-template")

    def test_unaddressable_cached_media_reference_fails_closed(self) -> None:
        self.backend.add(
            105,
            "proxylister-windows-build-105",
            extra=("args: -cdrom /var/lib/vz/template/iso/unmanaged.iso",),
        )
        with self.assertRaisesRegex(BuildError, "config references cached source media"):
            self.manager.remove_owned_clone(105, "proxylister-windows-build-105")
        self.assertNotIn(("qm", "destroy", "105", "--purge", "1"), self.backend.commands)


class SnapshotTests(unittest.TestCase):
    def _repository(self, parent: Path) -> Path:
        root = parent / "source"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "ProxyLister test"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "proxylister-test.invalid"], cwd=root, check=True
        )
        (root / "src").mkdir()
        (root / "src/input.txt").write_text("source\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/input.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "snapshot"], cwd=root, check=True)
        return root

    def test_dirty_snapshot_excludes_generated_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self._repository(parent)
            (root / "release/bin/windows").mkdir(parents=True)
            (root / "release/bin/windows/proxylister.exe").write_text("generated")
            (root / "release/.work/old").mkdir(parents=True)
            (root / "release/.work/old/log").write_text("generated")
            (root / "src/proxylister.egg-info").mkdir()
            (root / "src/proxylister.egg-info/PKG-INFO").write_text("generated")
            (root / "untracked.txt").write_text("included")
            snapshot = create_snapshot(root, parent / "work", release=False)
            verify_snapshot(snapshot)
            with tarfile.open(snapshot.archive) as archive:
                names = set(archive.getnames())
            self.assertIn("src/input.txt", names)
            self.assertIn("untracked.txt", names)
            self.assertFalse(any(name.startswith("release/bin") for name in names))
            self.assertFalse(any(name.startswith("release/.work") for name in names))
            self.assertFalse(any(".egg-info" in name for name in names))
            self.assertEqual(snapshot.tree, "dirty")

    def test_release_snapshot_rejects_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self._repository(parent)
            (root / "dirty-file").touch()
            with self.assertRaisesRegex(BuildError, "clean worktree"):
                create_snapshot(root, parent / "work", release=True)


class ArtifactTests(unittest.TestCase):
    def test_platform_promotion_preserves_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            windows = root / "bin/windows"
            source = root / "work/linux"
            windows.mkdir(parents=True)
            source.mkdir(parents=True)
            (windows / "proxylister.exe").write_text("windows")
            (source / "proxylister").write_text("linux")
            write_checksums(source, ["proxylister"])
            verify_checksums(source)
            promote_directory(source, root / "bin/linux")
            self.assertEqual((windows / "proxylister.exe").read_text(), "windows")
            self.assertEqual((root / "bin/linux/proxylister").read_text(), "linux")


class PublicationTests(unittest.TestCase):
    def _artifacts(self, root: Path, version: str = "1.0.1", commit: str = "a" * 40) -> None:
        for platform_name, executable in (
            ("linux", "proxylister"),
            ("windows", "proxylister.exe"),
        ):
            directory = root / "release/bin" / platform_name
            directory.mkdir(parents=True)
            (directory / executable).write_bytes(b"binary")
            (directory / "README.md").write_text("readme\n")
            (directory / "LICENSE").write_text("license\n")
            (directory / "MANIFEST.txt").write_text(
                f"version={version}\nsource_commit={commit}\nsource_tree=clean\n"
            )
            write_checksums(
                directory, [executable, "README.md", "LICENSE", "MANIFEST.txt"]
            )

    def test_packages_contain_each_verified_platform_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._artifacts(root)
            validate_release_artifacts(root, "1.0.1", "a" * 40)
            packages = create_release_packages(root, "1.0.1")

            self.assertEqual(packages[0].parent, root / "release/bin/packages")
            self.assertEqual(
                [path.name for path in packages],
                [
                    "proxylister-1.0.1-linux-x86_64.tar.gz",
                    "proxylister-1.0.1-windows-x86_64.zip",
                    "SHA256SUMS",
                ],
            )
            with tarfile.open(packages[0], "r:gz") as archive:
                self.assertIn(
                    "proxylister-1.0.1-linux-x86_64/proxylister",
                    archive.getnames(),
                )
            with zipfile.ZipFile(packages[1]) as archive:
                self.assertIn(
                    "proxylister-1.0.1-windows-x86_64/proxylister.exe",
                    archive.namelist(),
                )
            verify_checksums(packages[0].parent)

    def test_publication_rejects_artifacts_from_another_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._artifacts(root)
            with self.assertRaisesRegex(BuildError, "manifest source_commit"):
                validate_release_artifacts(root, "1.0.1", "b" * 40)

    def test_publication_creates_a_new_release_without_clobbering_assets(self) -> None:
        root = Path("/project")
        packages = [Path("/packages/linux.tar.gz"), Path("/packages/SHA256SUMS")]
        with patch.object(publish, "require_commands"), patch.object(
            publish, "git_identity", return_value=("b" * 40, "clean")
        ), patch.object(
            publish, "_project_version", return_value="1.0.1"
        ), patch.object(
            publish, "output", return_value="a" * 40
        ), patch.object(
            publish, "validate_release_artifacts"
        ) as validator, patch.object(
            publish, "create_release_packages", return_value=packages
        ), patch.object(
            publish, "run"
        ) as runner:
            self.assertEqual(publish.publish_release(root), packages)

        validator.assert_called_once_with(root, "1.0.1", "a" * 40)
        command = runner.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "release", "create", "v1.0.1"])
        self.assertIn("--verify-tag", command)
        self.assertIn("--generate-notes", command)
        self.assertNotIn("--clobber", command)


class WindowsProvisionTests(unittest.TestCase):
    def test_unverified_candidate_uses_same_media_guard_before_purge(self) -> None:
        backend = FakePVE()
        backend.add(
            9002,
            "proxylister-windows-template",
            template="1",
            protection="0",
            extra=(
                "ide0: local:iso/windows.iso,media=cdrom",
                "ide2: local:iso/virtio.iso,media=cdrom",
                "sata0: local-lvm:vm-9002-disk-0",
            ),
        )
        provisioner = WindowsProvisioner(RELEASE / "pve/windows", None, check_only=False)
        provisioner.backend = backend  # type: ignore[assignment]
        provisioner.pve = PVEManager(backend, protected_vmids={9000, 9001, 9002})
        with self.assertRaisesRegex(BuildError, "refusing to purge template"):
            provisioner.purge_candidate(9002, "proxylister-windows-template")
        provisioner.purge_candidate(
            9002, "proxylister-windows-template", allow_template=True
        )
        self.assertIn(("qm", "set", "9002", "--delete", "ide0"), backend.commands)
        self.assertIn(("qm", "set", "9002", "--delete", "ide2"), backend.commands)
        self.assertIn(("qm", "destroy", "9002", "--purge", "1"), backend.commands)

    def test_unattended_assets_keep_the_pinned_ready_state_contract(self) -> None:
        windows = RELEASE / "pve/windows"
        xml_text = (windows / "autounattend.xml").read_text(encoding="utf-8")
        document = ET.fromstring(xml_text)
        self.assertEqual(xml_text.count("@@PASSWORD@@"), 2)
        values = {element.tag.rsplit("}", 1)[-1]: element.text for element in document.iter()}
        computer_name = values["ComputerName"] or ""
        self.assertRegex(computer_name, r"^[A-Za-z0-9-]{1,15}$")
        self.assertFalse(computer_name.startswith("-") or computer_name.endswith("-"))
        self.assertEqual(values.get("SkipMachineOOBE"), "true")
        self.assertEqual(values.get("SkipUserOOBE"), "true")

        bootstrap = (windows / "bootstrap.ps1").read_text(encoding="utf-8")
        for filename, _, digest in WINDOWS_MEDIA[2:]:
            self.assertIn(filename, bootstrap)
            self.assertIn(digest, bootstrap.lower())
        self.assertIn('template_mode = "ready-state-v1"', bootstrap)
        self.assertIn("Stop-Computer -Force", bootstrap)
        self.assertNotRegex(bootstrap.lower(), r"sysprep\.exe|/generalize|add-windowscapability")

    def test_ready_marker_must_be_seen_before_candidate_shutdown(self) -> None:
        backend = FakePVE()
        backend.add(9002, "proxylister-windows-template", status="stopped")
        provisioner = WindowsProvisioner(RELEASE / "pve/windows", None, check_only=False)
        provisioner.backend = backend  # type: ignore[assignment]
        provisioner.pve = PVEManager(backend, protected_vmids={9000, 9001, 9002})
        with self.assertRaisesRegex(BuildError, "stopped before the template ready marker"):
            provisioner.wait_ready_shutdown(9002, 1)

        backend.vms[9002]["status"] = "running"
        backend.status_sequences[9002] = ["running", "stopped"]
        with patch("buildlib.provision.guest_powershell", return_value=True), patch(
            "buildlib.provision.time.sleep"
        ):
            provisioner.wait_ready_shutdown(9002, 2)


if __name__ == "__main__":
    unittest.main()
