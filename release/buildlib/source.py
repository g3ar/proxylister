"""Create one checksummed source snapshot for every native build guest."""

from __future__ import annotations

import os
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .core import BuildError, output, run, sha256


EXCLUDED_TOP_LEVEL = {".git", ".venv", "proxydb", "geodb"}
EXCLUDED_RELEASE = {PurePosixPath("release/.work"), PurePosixPath("release/bin")}


@dataclass(frozen=True)
class SourceSnapshot:
    archive: Path
    checksum: Path
    commit: str
    tree: str


def git_identity(root: Path) -> tuple[str, str]:
    commit = output(["git", "rev-parse", "HEAD"], cwd=root)
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise BuildError(f"git returned an invalid source commit: {commit}")
    state = "clean" if not output(["git", "status", "--porcelain"], cwd=root) else "dirty"
    return commit, state


def _excluded(relative: PurePosixPath) -> bool:
    if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL:
        return True
    if (
        "__pycache__" in relative.parts
        or relative.suffix == ".pyc"
        or any(part.endswith(".egg-info") for part in relative.parts)
    ):
        return True
    return any(relative == prefix or prefix in relative.parents for prefix in EXCLUDED_RELEASE)


def _dirty_archive(root: Path, archive: Path) -> None:
    with tarfile.open(archive, "w") as bundle:
        for current, directories, files in os.walk(root):
            current_path = Path(current)
            relative_dir = current_path.relative_to(root)
            directories[:] = sorted(
                name
                for name in directories
                if not _excluded(PurePosixPath((relative_dir / name).as_posix()))
            )
            for name in sorted(files):
                source = current_path / name
                relative = PurePosixPath(source.relative_to(root).as_posix())
                if not _excluded(relative):
                    bundle.add(source, arcname=relative.as_posix(), recursive=False)


def create_snapshot(root: Path, work: Path, *, release: bool) -> SourceSnapshot:
    commit, tree = git_identity(root)
    if release and tree != "clean":
        raise BuildError("release mode requires a clean worktree, including no untracked files")
    work.mkdir(parents=True, exist_ok=True)
    archive = work / "source.tar"
    checksum = work / "source.tar.sha256"
    if release:
        run(["git", "archive", "--format=tar", f"--output={archive}", "HEAD"], cwd=root)
    else:
        _dirty_archive(root, archive)
    digest = sha256(archive)
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return SourceSnapshot(archive, checksum, commit, tree)


def verify_snapshot(snapshot: SourceSnapshot) -> None:
    fields = snapshot.checksum.read_text(encoding="ascii").split()
    if len(fields) != 2 or fields[1] != snapshot.archive.name:
        raise BuildError("invalid source snapshot checksum file")
    if sha256(snapshot.archive) != fields[0].lower():
        raise BuildError("source snapshot checksum mismatch")
