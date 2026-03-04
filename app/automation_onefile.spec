# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(__file__).resolve().parents[1]

datas = [
    (str(project_root / "Codes"), "Codes"),
]

pricing_db = project_root / "pricing.db"
if pricing_db.exists():
    datas.append((str(pricing_db), "."))

price_lists = project_root / "PriceLists"
if price_lists.exists():
    datas.append((str(price_lists), "PriceLists"))

for dwg in project_root.glob("*.dwg"):
    datas.append((str(dwg), "."))

hiddenimports = [
    "openpyxl",
    "ezdxf",
    "ezdxf.addons.drawing",
    "ezdxf.addons.drawing.matplotlib",
    "matplotlib",
    "matplotlib.pyplot",
    "google",
    "google.genai",
    "PIL",
]

a = Analysis(
    [str(project_root / "run_app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AutomationPlatform",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
