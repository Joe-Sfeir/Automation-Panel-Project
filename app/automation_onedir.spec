# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).resolve().parent

datas = [(str(project_root / "Codes"), "Codes")]

pricing_db = project_root / "pricing.db"
if pricing_db.exists():
    datas.append((str(pricing_db), "."))

for folder_name in ("ODA", "assets"):
    folder_path = project_root / folder_name
    if folder_path.exists():
        datas.append((str(folder_path), folder_name))

for dwg in project_root.glob("*.dwg"):
    datas.append((str(dwg), "."))

blocked_data_names = {"app_config.json", "app_history.json"}
datas = [
    item for item in datas
    if Path(item[0]).name.lower() not in blocked_data_names
]

hiddenimports = [
    "openpyxl",
    "ezdxf",
    "ezdxf.recover",
    "ezdxf.addons.drawing",
    "ezdxf.addons.drawing.matplotlib",
    "matplotlib",
    "matplotlib.pyplot",
    "google",
    "google.genai",
    "google.genai.types",
    "PIL",
    "pymupdf",
] + collect_submodules("google.genai")

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
    [],
    exclude_binaries=True,
    name="TechnicatAutomation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TechnicatAutomation",
)
