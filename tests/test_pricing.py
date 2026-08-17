import sqlite3
import pytest

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
          (2, 20, 'FAZ-C16/1', NULL, 'MCB 1P 16A curve C 10kA', 12.0),
          (3, 10, 'A9F74120', NULL, 'MCB 1P 20A curve C 6kA', 11.0),
          (4, 10, 'ISW40', NULL, 'iSW switch 4P 40A', 20.0),
          (5, 10, 'INS40', NULL, 'INS40 switch disconnector 4P 40A', 30.0),
          (6, 10, 'RCCB25', NULL, 'RCCB 4P 25A 30mA AC', 40.0),
          (7, 10, 'MCB-B16', NULL, 'MCB 1P 16A curve B 6kA', 9.0),
          (8, 10, 'HANDLE40', NULL, 'Extended rotary handle INS 40A', 5.0);
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
    assert payload["grand_ht"] is None
    assert payload["grand_ttc"] is None
    assert payload["totals_by_currency"]["EUR"]["grand_ht"] == 20.0
    assert payload["totals_by_currency"]["USD"]["grand_ht"] == 12.0


def test_pricing_includes_extracted_rows_without_approval_and_skips_excluded(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    project = ProjectDocument.create("Test")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    included = Component("c1", category="MCB", manufacturer="SCHNEIDER")
    excluded = Component("c2", category="MCB", manufacturer="SCHNEIDER", review_status="excluded")
    project.panels.append(Panel("panel", "P1", "", components=[included, excluded]))

    PricingService(database).price_project(project, tmp_path / "pricing.json")
    assert [item.component_id for item in project.match_results] == ["c1"]


def test_component_manufacturer_remains_authoritative_with_one_project_edition(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    project = ProjectDocument.create("Test")
    project.pricing.default_manufacturer = "SCHNEIDER"
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    project.panels.append(Panel("panel", "P1", "", components=[
        Component("c1", category="MCB", poles="1P", amps="16A", curve="C", manufacturer="LEGRAND"),
    ]))

    PricingService(database).price_project(project, tmp_path / "pricing.json")

    assert project.match_results[0].manufacturer == "LEGRAND"
    assert project.match_results[0].chosen_reference == ""
    assert project.match_results[0].note == "Price-list edition is not installed"


def test_pricing_can_be_cancelled_before_catalog_work(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    project = ProjectDocument.create("Test")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    project.panels.append(Panel("panel", "P1", "", components=[Component("c1")]))

    with pytest.raises(InterruptedError, match="cancelled"):
        PricingService(database, should_cancel=lambda: True).price_project(
            project, tmp_path / "pricing.json"
        )


def test_structured_specs_reject_wrong_breaker_rating(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    service = PricingService(database)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        aliases = {str(row["alias"]).lower(): str(row["canonical"]).lower() for row in connection.execute("SELECT alias, canonical FROM term_aliases")}
        result = service._match(
            connection, aliases,
            Component("c", category="MCB", poles="1P", amps="16A", curve="C"),
            "SCHNEIDER", 2026, "H1", {},
        )
    assert result.chosen_reference == "A9F74116"
    assert all(item.reference != "A9F74120" for item in result.alternatives)
    assert all(item.reference != "MCB-B16" for item in result.alternatives)


def test_equivalent_switch_families_are_not_silently_auto_selected(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    service = PricingService(database)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        aliases = {str(row["alias"]).lower(): str(row["canonical"]).lower() for row in connection.execute("SELECT alias, canonical FROM term_aliases")}
        result = service._match(
            connection, aliases,
            Component("c", category="SWITCH", poles="4P", amps="40A"),
            "SCHNEIDER", 2026, "H1", {},
        )
    assert result.review_status == "check_candidate"
    assert "equivalent" in result.note.lower()
    assert all(item.reference != "HANDLE40" for item in result.alternatives)


def test_breaker_only_curve_and_breaking_capacity_do_not_block_switch_matching(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    service = PricingService(database)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        aliases = {
            str(row["alias"]).lower(): str(row["canonical"]).lower()
            for row in connection.execute("SELECT alias, canonical FROM term_aliases")
        }
        result = service._match(
            connection,
            aliases,
            Component(
                "c", category="SWITCH", poles="4P", amps="40A",
                curve="C", breaking_capacity="6kA",
            ),
            "SCHNEIDER",
            2026,
            "H1",
            {},
        )

    assert result.chosen_reference in {"ISW40", "INS40"}
    assert result.review_status == "check_candidate"


def test_rccb_rating_is_never_upsized_during_pricing(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    service = PricingService(database)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        aliases = {str(row["alias"]).lower(): str(row["canonical"]).lower() for row in connection.execute("SELECT alias, canonical FROM term_aliases")}
        result = service._match(
            connection, aliases,
            Component("c", category="RCCB", poles="4P", amps="16A", sensitivity="30mA"),
            "SCHNEIDER", 2026, "H1", {},
        )
    assert result.chosen_reference == ""
    assert result.review_status == "unmatched"
    assert all(item.reference != "RCCB25" for item in result.alternatives)


def test_real_rccb_10a_case_recommends_25a_but_rejects_10a_rcbo(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO products VALUES (9, 10, 'A9D34610', NULL, ?, 91.17)",
            ("IDPNA VIGI 10A C 30MA AC-TYPE RESIDUAL CIRCUIT BREAKER",),
        )
        connection.commit()

    service = PricingService(database)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        aliases = {
            str(row["alias"]).lower(): str(row["canonical"]).lower()
            for row in connection.execute("SELECT alias, canonical FROM term_aliases")
        }
        result = service._match(
            connection,
            aliases,
            Component(
                "c", category="RCCB", poles="4P", amps="10A", sensitivity="30mA"
            ),
            "SCHNEIDER",
            2026,
            "H1",
            {},
        )

    assert result.review_status == "unmatched"
    assert result.chosen_reference == ""
    references = {item.reference for item in result.recommendations}
    assert "RCCB25" in references
    assert "A9D34610" not in references
    candidate = next(item for item in result.recommendations if item.reference == "RCCB25")
    assert candidate.recommendation_kind == "same_manufacturer_nearest"
    assert candidate.requires_engineering_review


def test_other_manufacturer_exact_rating_is_recommended_not_auto_selected(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO products VALUES (9, 20, 'PFIM-10/4/003', NULL, ?, 72.0)",
            ("RCCB 4P 10A 30mA AC",),
        )
        connection.commit()
    project = ProjectDocument.create("Cross-brand recommendations")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    project.panels.append(Panel("panel", "P1", "", components=[
        Component("c", category="RCCB", poles="4P", amps="10A", sensitivity="30mA")
    ]))

    PricingService(database).price_project(project, tmp_path / "pricing.json")

    result = project.match_results[0]
    assert result.review_status == "unmatched"
    candidate = next(
        item for item in result.recommendations if item.reference == "PFIM-10/4/003"
    )
    assert candidate.manufacturer == "EATON"
    assert candidate.recommendation_kind == "other_manufacturer_exact"
    assert not candidate.requires_engineering_review


def test_spd_type_is_a_hard_catalog_constraint(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO manufacturers VALUES (3, 'PHOENIX CONTACT')")
        connection.execute("INSERT INTO ppl_editions VALUES (30, 3, 2026, 'H1', 'USD')")
        connection.executemany(
            "INSERT INTO products VALUES (?, 30, ?, NULL, ?, ?)",
            [
                (20, "T2", "Three phase surge voltage arrester Type 2", 100.0),
                (21, "T12", "Three phase lightning current and surge arrester combination Type I + II", 90.0),
                (22, "T3", "Three phase surge voltage arrester Type 3", 80.0),
            ],
        )
        connection.commit()
    project = ProjectDocument.create("SPD")
    project.pricing.editions = {"PHOENIX CONTACT": {"year": 2026, "half": "H1"}}
    project.panels.append(Panel("panel", "P1", "", components=[
        Component("spd", category="SPD", spd_type="TYPE 2", manufacturer="PHOENIX CONTACT")
    ]))

    PricingService(database).price_project(project, tmp_path / "pricing.json")

    result = project.match_results[0]
    assert result.chosen_reference == "T2"
    assert {item.reference for item in result.alternatives} <= {"T2"}
    assert all(item.reference not in {"T12", "T3"} for item in result.recommendations)


def test_configured_family_preference_breaks_an_exact_spec_tie(tmp_path):
    database = tmp_path / "pricing.db"
    make_database(database)
    project = ProjectDocument.create("Preferred family")
    project.pricing.editions = {"SCHNEIDER": {"year": 2026, "half": "H1"}}
    project.pricing.family_preferences = {"SCHNEIDER:SWITCH": ["ISW", "INS"]}
    project.panels.append(Panel("panel", "P1", "", components=[
        Component("switch", category="SWITCH", poles="4P", amps="40A", manufacturer="SCHNEIDER")
    ]))

    PricingService(database).price_project(project, tmp_path / "pricing.json")

    result = project.match_results[0]
    assert result.chosen_reference == "ISW40"
    assert result.review_status == "exact_match"
