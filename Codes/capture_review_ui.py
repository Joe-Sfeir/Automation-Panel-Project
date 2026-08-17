from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.main import APP_STYLESHEET
from app.review_ui import ProjectReviewDialog


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the desktop component review surface")
    parser.add_argument("project")
    parser.add_argument("--database", default="pricing.db")
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=1460)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--focus", action="store_true")
    parser.add_argument("--pricing-state", action="store_true")
    parser.add_argument("--select-unmatched", action="store_true")
    parser.add_argument("--select-category", default="")
    parser.add_argument("--offer-tab", action="store_true")
    arguments = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    dialog = ProjectReviewDialog(arguments.project, arguments.database)
    dialog.resize(arguments.width, arguments.height)
    dialog.show()
    if arguments.pricing_state:
        dialog._set_pricing_running(True)
    if arguments.offer_tab:
        dialog.lower_tabs.setCurrentIndex(2)
    if (arguments.select_unmatched or arguments.select_category) and dialog.project.panels:
        unmatched_ids = {
            row.component_id
            for row in dialog.project.match_results
            if row.review_status == "unmatched"
        }
        panel = dialog.project.panels[dialog._panel_index]
        row = next(
            (index for index, component in enumerate(panel.components)
             if (not arguments.select_unmatched or component.id in unmatched_ids)
             and (
                 not arguments.select_category
                 or component.category.upper() == arguments.select_category.upper()
             )),
            -1,
        )
        if row >= 0:
            dialog.components.selectRow(row)
            dialog._show_selected_component()
    if arguments.focus:
        dialog.include_button.setFocus(Qt.TabFocusReason)
    app.processEvents()
    output = Path(arguments.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not dialog.grab().save(str(output)):
        raise RuntimeError(f"Could not write screenshot: {output}")
    dialog.close()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
