from PySide6.QtCore import Qt

from app.main import ProjectPage


def test_project_page_uses_explicit_brand_checkboxes(qtbot):
    page = ProjectPage()
    qtbot.addWidget(page)
    page.set_brands(["EATON", "LEGRAND", "SCHNEIDER"])
    states = {page.brand_list.item(index).text(): page.brand_list.item(index).checkState() for index in range(3)}
    assert states["SCHNEIDER"] == Qt.Checked
    assert states["EATON"] == Qt.Unchecked
