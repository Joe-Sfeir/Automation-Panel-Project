from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from app.main import APP_STYLESHEET, MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the native desktop application")
    parser.add_argument("--page", choices=["overview", "project", "history", "pricing"], default="project")
    parser.add_argument("--out", required=True)
    arguments = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    window = MainWindow()
    {
        "overview": window._nav_to_home,
        "project": window._nav_to_project,
        "history": window._nav_to_history,
        "pricing": window._nav_to_pricing_db,
    }[arguments.page]()
    window.resize(1360, 860)
    window.show()
    app.processEvents()
    output = Path(arguments.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not window.grab().save(str(output)):
        raise RuntimeError(f"Could not write screenshot: {output}")
    window.close()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
