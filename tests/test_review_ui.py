from app.domain import Component, Panel, ProjectDocument
from app.persistence import save_project
from app.review_ui import COMPONENT_COLUMNS, ProjectReviewDialog
from PySide6.QtCore import Qt


def test_review_dialog_approval_unlocks_pricing(qtbot, tmp_path):
    project = ProjectDocument.create("Review test")
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("c1", category="MCB", label="Q1", manufacturer="SCHNEIDER"),
    ]))
    project_path = tmp_path / "project.technelec.json"
    save_project(project, project_path)

    dialog = ProjectReviewDialog(project_path, tmp_path / "pricing.db")
    qtbot.addWidget(dialog)
    dialog.show()

    assert not dialog.price_button.isEnabled()
    dialog.components.selectRow(0)
    qtbot.mouseClick(dialog.approve_button, Qt.LeftButton)
    assert dialog.price_button.isEnabled()
    assert dialog.project.panels[0].components[0].review_status == "approved"


def test_review_dialog_edit_returns_approved_row_to_review(qtbot, tmp_path):
    project = ProjectDocument.create("Review test")
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("c1", category="MCB", label="Q1", quantity=1, review_status="approved"),
    ]))
    project_path = tmp_path / "project.technelec.json"
    save_project(project, project_path)
    dialog = ProjectReviewDialog(project_path, tmp_path / "pricing.db")
    qtbot.addWidget(dialog)

    quantity_column = next(index for index, (_, field) in enumerate(COMPONENT_COLUMNS) if field == "quantity")
    dialog.components.item(0, quantity_column).setText("2")
    dialog.save()

    assert dialog.project.panels[0].components[0].quantity == 2
    assert dialog.project.panels[0].components[0].review_status == "needs_review"
    assert not dialog.price_button.isEnabled()
