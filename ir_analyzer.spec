# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH)
APP_DIR = ROOT / "ir_analyzer"
APP_NAME = "In Situ IR Analyzer"
ICON_DIR = ROOT / "assets" / "icons"
ICON_FILE = (
    ICON_DIR / "in-situ-ir-analyzer.icns"
    if sys.platform == "darwin"
    else ICON_DIR / "in-situ-ir-analyzer.ico"
)

hiddenimports = []
hiddenimports += ["numpy.core", "numpy.core.multiarray"]
hiddenimports += collect_submodules("scipy.stats")
hiddenimports += collect_submodules("scipy._lib")

datas = []
datas += collect_data_files("pyqtgraph")


a = Analysis(
    [str(APP_DIR / "main.py")],
    pathex=[str(APP_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    exclude_binaries=True,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=str(ICON_FILE),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(ICON_FILE),
        bundle_identifier="com.kist.ir-analyzer",
    )
