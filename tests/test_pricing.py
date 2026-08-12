import sqlite3

from app.domain import Component, Panel, ProjectDocument
from app.pricing import PricingService


def make_database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE ppl_editions (id INTEGER PRIMARY KEY, manufacturer_id INTEGER, year INTEGER, half TEXT, currency TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, ppl_edition_id INTEGER, reference TEXT, order_number TEXT, description TEXT, price REAL);
        CREATE TABLE term_aliases (alias TEXT, canonical TEXT);
        INSERT INTO manufacturers VALUES (1, 'SCHNEIDER'), (2, 'EATON');
        INSERT INTO ppl_editions VALUES (10, 1, 2026, 'H1', 'EUR'), (20, 2, 2026, 'H1', 'USD');
        INSERT INTO products VALUES
          (1, 10, 'A9F74116', NULL, 'MCB 1P 16A curve C 6kA', 10.0),
          (2, 20, 'FAZ-C16/1', NULL, 'MCB 1P 16A curve C 10kA', 12.0);
    """)
    connection.commit()
    connection.close()


def test_component_manufacturer_and_forced_reference_are_respected(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    project = ProjectDocument.create("Test")
    project.pricing.default_manufacturer = "SCHNEIDER"
    project.pricing.editions = {
        "SCHNEIDER": {"year": 2026, "half": "H1"},
        "EATON": {"year": 2026, "half": "H1"},
    }
    project.panels.append(Panel("panel", "P1", "", components=[
        Component("c1", category="MCB", poles="1P", amps="16A", curve="C", manufacturer="SCHNEIDER", forced_reference="A9F74116", quantity=2, review_status="approved"),
        Component("c2", category="MCB", poles="1P", amps="16A", curve="C", manufacturer="EATON", review_status="approved"),
    ]))
    payload = PricingService(database).price_project(project, tmp_path / "pricing.json")
    by_id = {item.component_id: item for item in project.match_results}
    assert by_id["c1"].tier == "FORCED"
    assert by_id["c1"].total_price == 20.0
    assert by_id["c2"].manufacturer == "EATON"
    assert by_id["c2"].currency == "USD"
    assert payload["currencies"] == ["EUR", "USD"]


def test_pricing_requires_explicit_review_and_skips_excluded(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    project = ProjectDocument.create("Test")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    included = Component("c1", category="MCB", manufacturer="SCHNEIDER")
    excluded = Component("c2", category="MCB", manufacturer="SCHNEIDER", review_status="excluded")
    project.panels.append(Panel("panel", "P1", "", components=[included, excluded]))

    try:
        PricingService(database).price_project(project, tmp_path / "pricing.json")
    except ValueError as exc:
        assert "blocked" in str(exc).lower()
    else:
        raise AssertionError("pricing should require review")

    included.review_status = "approved"
    PricingService(database).price_project(project, tmp_path / "pricing.json")
    assert [item.component_id for item in project.match_results] == ["c1"]
