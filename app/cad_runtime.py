from __future__ import annotations

import glob
import platform
import shutil
import subprocess
from pathlib import Path


def find_oda_converter() -> str | None:
    """Return an installed ODA File Converter executable, when available."""
    system = platform.system()
    if system == "Windows":
        patterns = [
            r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
            r"C:\Program Files\ODA\ODAFileConverter 26.12.0\ODAFileConverter.exe",
            r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe",
        ]
        for pattern in patterns:
            matches = glob.glob(pattern)
            if matches:
                return matches[0]
    elif system == "Darwin":
        matches = glob.glob("/Applications/ODAFileConverter*.app/Contents/MacOS/ODAFileConverter")
        if matches:
            return matches[0]
    else:
        for candidate in ("/usr/bin/ODAFileConverter", "/usr/local/bin/ODAFileConverter"):
            if Path(candidate).exists():
                return candidate
    return None


def convert_dwg_to_dxf(dwg_path: Path, oda_path: str | None, temporary_dir: Path) -> Path:
    """Convert one DWG to DXF without importing any AI or credential code."""
    converter = oda_path or find_oda_converter()
    if not converter or not Path(converter).exists():
        raise RuntimeError(
            "ODA File Converter was not found. Install it or configure its path in Preferences. "
            "Alternatively, provide a DXF file directly."
        )

    input_dir = temporary_dir / "in"
    output_dir = temporary_dir / "out"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dwg_path, input_dir / dwg_path.name)

    kwargs: dict = {}
    if platform.system() == "Windows":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = 0
        kwargs["startupinfo"] = startup_info

    try:
        result = subprocess.run(
            [str(converter), str(input_dir), str(output_dir), "ACAD2018", "DXF", "0", "1"],
            capture_output=True,
            text=True,
            timeout=120,
            **kwargs,
        )
    except PermissionError as exc:
        raise RuntimeError(
            "ODA File Converter could not be started because Windows denied access."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ODA File Converter timed out after 120 seconds.") from exc
    except OSError as exc:
        raise RuntimeError(f"ODA File Converter could not be started: {exc}") from exc

    converted = sorted(output_dir.glob("*.dxf"))
    if not converted:
        details = (result.stderr or result.stdout or "").strip()
        suffix = f" Details: {details[-500:]}" if details else ""
        raise RuntimeError(
            f"ODA File Converter produced no DXF output (exit code {result.returncode}).{suffix}"
        )
    return converted[0]


def render_dxf_png(dxf_path: Path, output_png: Path, dpi: int = 150) -> Path | None:
    """Render a DXF to PNG using only local CAD/rendering dependencies."""
    try:
        import ezdxf
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

        document = ezdxf.readfile(str(dxf_path))
        modelspace = document.modelspace()
        for hatch in list(modelspace.query("HATCH")):
            modelspace.delete_entity(hatch)

        output_png.parent.mkdir(parents=True, exist_ok=True)
        figure = plt.figure(figsize=(24, 18), dpi=dpi)
        try:
            axes = figure.add_axes([0, 0, 1, 1])
            axes.set_aspect("equal")
            context = RenderContext(document)
            backend = MatplotlibBackend(axes)
            Frontend(context, backend).draw_layout(modelspace, finalize=True)
            figure.savefig(
                str(output_png), dpi=dpi, bbox_inches="tight", facecolor="white", format="png"
            )
        finally:
            plt.close(figure)
        return output_png
    except Exception:
        return None
