from app.domain import Component, Panel, ProjectDocument
from app.persistence import save_project
from app.review_ui import COMPONENT_COLUMNS, ProjectReviewDialog
from PySide6.QtCore import Qt


def test_review_dialog_allows_direct_pricing_and_optional_exclusion(qtbot, tmp_path):
    project = ProjectDocument.create("Review test")
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("c1", category="MCB", label="Q1", manufacturer="SCHNEIDER"),
    ]))
    project_path = tmp_path / "project.technelec.json"
    save_project(project, project_path)

    dialog = ProjectReviewDialog(project_path, tmp_path / "pricing.db")
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.price_button.isEnabled()
    assert dialog.price_button.text() == "Price & export offer"
    dialog.components.selectRow(0)
    qtbot.mouseClick(dialog.exclude_button, Qt.LeftButton)
    assert not dialog.price_button.isEnabled()
    assert dialog.project.panels[0].components[0].review_status == "excluded"
    qtbot.mouseClick(dialog.include_button, Qt.LeftButton)
    assert dialog.price_button.isEnabled()
    assert dialog.project.panels[0].components[0].review_status == "included"


def test_review_dialog_edit_remains_directly_priceable(qtbot, tmp_path):
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
    assert dialog.project.panels[0].components[0].review_status == "included"
    assert dialog.price_button.isEnabled()
