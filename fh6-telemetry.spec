# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone FH6 telemetry executable.

Builds a single-file executable from ``launcher.py`` that bundles the FastAPI
app, the static dashboard, and all Python dependencies. Build with:

    pyinstaller fh6-telemetry.spec

The output is ``dist/fh6-telemetry`` (``dist/fh6-telemetry.exe`` on Windows).

Notes
-----
* The ``app/static`` directory is shipped as data files and resolved at runtime
  via ``sys._MEIPASS`` (see ``app.main._static_dir``).
* uvicorn/anyio load parts of themselves dynamically, so we collect their
  submodules explicitly to avoid "module not found" errors in the frozen app.
* ``pyarrow`` is excluded on purpose: it is large and only powers the optional
  Parquet raw format. CSV (the default) needs nothing extra, keeping the exe
  lean. Set ``FH6_RAW_FORMAT=csv`` (the default) when running the exe.
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
for pkg in ("uvicorn", "anyio", "fastapi", "starlette", "websockets"):
    hiddenimports += collect_submodules(pkg)

datas = [
    ("app/static", "app/static"),
]

excludes = [
    "pyarrow",        # optional Parquet backend; CSV default needs none of it
    "tkinter",
    "pytest",
    "matplotlib",
]


a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="fh6-telemetry",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # keep the console so users see the access URL
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
