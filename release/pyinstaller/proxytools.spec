# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller definition for the native Linux one-file executable."""

from pathlib import Path
import os


ROOT = Path(SPECPATH).resolve().parents[1]
BUILD_INFO = os.environ.get("PROXYTOOLS_BUILD_INFO")
if not BUILD_INFO:
    raise RuntimeError("PROXYTOOLS_BUILD_INFO is required")

analysis = Analysis(
    [str(ROOT / "src/proxytools/__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "proxytools.conf"), "."),
        (BUILD_INFO, "."),
    ],
    hiddenimports=["proxytools.commands.list", "proxytools.commands.monitor"],
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
    name="proxytools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
