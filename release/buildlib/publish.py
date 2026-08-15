"""Package verified native artifacts and publish one tagged GitHub release."""

from __future__ import annotations

import re
import tarfile
import zipfile
from pathlib import Path

from .core import (
    BuildError,
    output,
    remove_tree,
    require_commands,
    run,
    verify_checksums,
    write_checksums,
)
from .source import git_identity


PLATFORMS = {
    "linux": ("x86_64", ".tar.gz"),
    "windows": ("x86_64", ".zip"),
}
PACKAGE_FILES = {
    "linux": ("proxylister", "README.md", "LICENSE", "MANIFEST.txt", "SHA256SUMS"),
    "windows": ("proxylister.exe", "README.md", "LICENSE", "MANIFEST.txt", "SHA256SUMS"),
}


def _project_version(root: Path) -> str:
    text = (root / "src/proxylister/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', text, re.MULTILINE)
    if not match:
        raise BuildError("could not read a stable project version")
    return match.group(1)


def _manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            raise BuildError(f"invalid artifact manifest line: {line}")
        key, value = line.split("=", 1)
        values[key] = value
    return values


def validate_release_artifacts(root: Path, version: str, commit: str) -> None:
    for platform_name, names in PACKAGE_FILES.items():
        directory = root / "release/bin" / platform_name
        if not directory.is_dir():
            raise BuildError(f"{platform_name} release artifacts are missing")
        verify_checksums(directory)
        missing = [name for name in names if not (directory / name).is_file()]
        if missing:
            raise BuildError(f"{platform_name} release files are missing: {', '.join(missing)}")
        manifest = _manifest(directory / "MANIFEST.txt")
        expected = {
            "version": version,
            "source_commit": commit,
            "source_tree": "clean",
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise BuildError(
                    f"{platform_name} manifest {key} is {manifest.get(key)!r}, expected {value!r}"
                )


def create_release_packages(root: Path, version: str) -> list[Path]:
    destination = root / "release/bin/packages"
    remove_tree(destination)
    destination.mkdir(parents=True)
    packages: list[Path] = []
    for platform_name, (architecture, suffix) in PLATFORMS.items():
        stem = f"proxylister-{version}-{platform_name}-{architecture}"
        package = destination / f"{stem}{suffix}"
        source = root / "release/bin" / platform_name
        if platform_name == "linux":
            with tarfile.open(package, "w:gz") as archive:
                for name in PACKAGE_FILES[platform_name]:
                    archive.add(source / name, arcname=f"{stem}/{name}")
        else:
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in PACKAGE_FILES[platform_name]:
                    archive.write(source / name, arcname=f"{stem}/{name}")
        packages.append(package)
    write_checksums(destination, [package.name for package in packages])
    return [*packages, destination / "SHA256SUMS"]


def publish_release(root: Path) -> list[Path]:
    require_commands(("git", "gh"))
    _commit, tree = git_identity(root)
    if tree != "clean":
        raise BuildError("publication requires a clean worktree")
    version = _project_version(root)
    tag = f"v{version}"
    tagged_commit = output(["git", "rev-list", "-n", "1", tag], cwd=root)
    if len(tagged_commit) != 40:
        raise BuildError(f"tag {tag} does not resolve to a commit")
    validate_release_artifacts(root, version, tagged_commit)
    packages = create_release_packages(root, version)
    run(
        [
            "gh",
            "release",
            "create",
            tag,
            *packages,
            "--verify-tag",
            "--title",
            f"ProxyLister {version}",
            "--generate-notes",
        ],
        cwd=root,
    )
    return packages
