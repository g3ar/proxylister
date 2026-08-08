# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller definition for the native Linux one-file executable."""

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]

analysis = Analysis(
    [str(ROOT / "src/proxytools/__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[(str(ROOT / "proxytools.conf"), ".")],
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
