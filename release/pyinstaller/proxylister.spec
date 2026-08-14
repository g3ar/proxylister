# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller definition shared by the native Linux and Windows executables."""

from pathlib import Path
import os


ROOT = Path(SPECPATH).resolve().parents[1]
BUILD_INFO = os.environ.get("PROXYLISTER_BUILD_INFO")
if not BUILD_INFO:
    raise RuntimeError("PROXYLISTER_BUILD_INFO is required")

analysis = Analysis(
    [str(ROOT / "src/proxylister/__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "proxylister.conf"), "."),
        (BUILD_INFO, "."),
    ],
    hiddenimports=["proxylister.commands.list", "proxylister.commands.monitor"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="proxylister",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
