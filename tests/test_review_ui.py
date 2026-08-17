import time
import sqlite3
from types import SimpleNamespace

from app.domain import Component, MatchResult, Panel, ProjectDocument
from app.persistence import load_project, save_project
from app.review_ui import COMPONENT_COLUMNS, PricingExportWorker, ProjectReviewDialog
from PySide6.QtCore import QObject, Qt, QThread, Signal


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
    assert dialog.price_button.text() == "Apply & reprice"
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


def test_offer_manufacturer_dropdown_clears_stale_reference_and_result(qtbot, tmp_path):
    database = tmp_path / "pricing.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE ppl_editions (id INTEGER PRIMARY KEY, manufacturer_id INTEGER, year INTEGER, half TEXT, currency TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, ppl_edition_id INTEGER, reference TEXT, order_number TEXT, description TEXT, price REAL);
        CREATE TABLE term_aliases (alias TEXT, canonical TEXT);
        INSERT INTO manufacturers VALUES (1, 'SCHNEIDER'), (2, 'EATON');
        INSERT INTO ppl_editions VALUES (10, 1, 2026, 'H1', 'EUR'), (20, 2, 2026, 'H1', 'USD');
    """)
    connection.commit()
    connection.close()
    project = ProjectDocument.create("Dropdown")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    component = Component(
        "c1", category="MCB", manufacturer="SCHNEIDER",
        forced_reference="A9F74116",
    )
    project.panels.append(Panel("p1", "P1", "", components=[component]))
    project.match_results = [
        MatchResult(
            "c1", "SCHNEIDER", "SCHNEIDER 2026 H1",
            chosen_reference="A9F74116", unit_price=10.0,
            quantity=1, total_price=10.0, currency="EUR",
            review_status="exact_match",
        )
    ]
    project_path = tmp_path / "project.technelec.json"
    save_project(project, project_path)

    dialog = ProjectReviewDialog(project_path, database)
    qtbot.addWidget(dialog)
    dialog.lower_tabs.setCurrentIndex(2)
    combo = dialog.matches.cellWidget(0, 1)
    assert combo is not None
    combo.setCurrentText("EATON")

    changed = dialog.project.panels[0].components[0]
    assert changed.manufacturer == "EATON"
    assert changed.forced_reference == ""
    assert dialog.project.match_results == []
    assert dialog._pricing_stale is True


def test_pricing_starts_without_blocking_the_ui(qtbot, tmp_path, monkeypatch):
    class SlowSuccessfulWorker(QObject):
        progress = Signal(str)
        finished = Signal(str)
        failed = Signal(str)
        cancelled = Signal()

        def __init__(self, project_path, database):
            super().__init__()
            self.project_path = project_path

        def run(self):
            self.progress.emit("Testing background work")
            time.sleep(0.2)
            self.finished.emit(str(tmp_path / "pricing.json"))

    project = ProjectDocument.create("Review test")
    project.panels.append(Panel("p1", "P1", "", components=[Component("c1")]))
    project_path = tmp_path / "project.technelec.json"
    save_project(project, project_path)
    monkeypatch.setattr("app.review_ui.PricingExportWorker", SlowSuccessfulWorker)
    monkeypatch.setattr("app.review_ui.QMessageBox.information", lambda *args: None)

    dialog = ProjectReviewDialog(project_path, tmp_path / "pricing.db")
    qtbot.addWidget(dialog)
    started = time.perf_counter()
    dialog.save_price_export()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    assert dialog.price_button.text() == "Pricing…"
    assert dialog.cancel_price_button.isEnabled()
    qtbot.waitUntil(lambda: dialog._pricing_thread is None, timeout=3000)
    assert dialog.price_button.text() == "Apply & reprice"


def test_real_pricing_worker_stops_at_reviewable_pricing_snapshot(qtbot, tmp_path):
    database = tmp_path / "pricing.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE ppl_editions (id INTEGER PRIMARY KEY, manufacturer_id INTEGER, year INTEGER, half TEXT, currency TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, ppl_edition_id INTEGER, reference TEXT, order_number TEXT, description TEXT, price REAL);
        CREATE TABLE term_aliases (alias TEXT, canonical TEXT);
        INSERT INTO manufacturers VALUES (1, 'SCHNEIDER');
        INSERT INTO ppl_editions VALUES (10, 1, 2026, 'H1', 'EUR');
        INSERT INTO products VALUES (1, 10, 'A9F74116', NULL, 'MCB 1P 16A curve C 6kA', 10.0);
    """)
    connection.commit()
    connection.close()
    project = ProjectDocument.create("Worker test")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("c1", category="MCB", poles="1P", amps="16A", curve="C"),
    ]))
    project_path = tmp_path / "project.technelec.json"
    save_project(project, project_path)

    thread = QThread()
    worker = PricingExportWorker(project_path, database)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.cancelled.connect(thread.quit)
    with qtbot.waitSignal(worker.finished, timeout=5000) as result:
        thread.start()
    qtbot.waitUntil(lambda: not thread.isRunning(), timeout=3000)

    assert result.args[0].endswith("pricing.json")
    assert (tmp_path / "pricing.json").exists()
    assert not (tmp_path / "offer.xlsx").exists()
    worker.deleteLater()
    thread.deleteLater()


def test_pricing_worker_automatically_applies_ai_shortlist_as_review_item(
    qtbot, tmp_path, monkeypatch
):
    database = tmp_path / "pricing.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE ppl_editions (id INTEGER PRIMARY KEY, manufacturer_id INTEGER, year INTEGER, half TEXT, currency TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, ppl_edition_id INTEGER, reference TEXT, order_number TEXT, description TEXT, price REAL);
        CREATE TABLE term_aliases (alias TEXT, canonical TEXT);
        INSERT INTO manufacturers VALUES (1, 'SCHNEIDER');
        INSERT INTO ppl_editions VALUES (10, 1, 2026, 'H1', 'EUR');
        INSERT INTO products VALUES (1, 10, 'RCCB25', NULL, 'RCCB 4P 25A 30mA AC', 40.0);
    """)
    connection.commit()
    connection.close()
    project = ProjectDocument.create("AI fallback")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    project.panels.append(Panel("p1", "P1", "", components=[
        Component(
            "c1", category="RCCB", poles="4P", amps="10A",
            sensitivity="30mA", manufacturer="SCHNEIDER",
        ),
    ]))
    project_path = tmp_path / "project.technelec.json"
    save_project(project, project_path)

    def choose_first(_database, _component, candidates):
        return candidates[0], SimpleNamespace(summary="Nearest available rating"), False

    monkeypatch.setattr("app.review_ui.ask_ai_for_recommendation", choose_first)
    worker = PricingExportWorker(project_path, database)
    failures = []
    worker.failed.connect(failures.append)
    worker.run()

    assert failures == []
    priced = load_project(project_path)
    component = priced.panels[0].components[0]
    assert component.forced_reference == "RCCB25"
    assert component.ai_review_required is True
    assert priced.match_results[0].review_status == "ai_suggested"
    assert "verify before export" in priced.match_results[0].note


def test_offer_export_is_a_separate_review_action(qtbot, tmp_path, monkeypatch):
    database = tmp_path / "pricing.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE ppl_editions (id INTEGER PRIMARY KEY, manufacturer_id INTEGER, year INTEGER, half TEXT, currency TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, ppl_edition_id INTEGER, reference TEXT, order_number TEXT, description TEXT, price REAL);
        CREATE TABLE term_aliases (alias TEXT, canonical TEXT);
        INSERT INTO manufacturers VALUES (1, 'SCHNEIDER');
        INSERT INTO ppl_editions VALUES (10, 1, 2026, 'H1', 'EUR');
        INSERT INTO products VALUES (1, 10, 'A9F74116', NULL, 'MCB 1P 16A curve C 6kA', 10.0);
    """)
    connection.commit()
    connection.close()
    project = ProjectDocument.create("Export test")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("c1", category="MCB", poles="1P", amps="16A", curve="C"),
    ]))
    project_path = tmp_path / "project.technelec.json"
    save_project(project, project_path)

    service_worker = PricingExportWorker(project_path, database)
    service_worker.run()
    assert (tmp_path / "pricing.json").exists()
    assert not (tmp_path / "offer.xlsx").exists()

    monkeypatch.setattr("app.review_ui.QMessageBox.information", lambda *args: None)
    dialog = ProjectReviewDialog(project_path, database)
    qtbot.addWidget(dialog)
    dialog.export_offer()

    assert (tmp_path / "offer.xlsx").exists()
