import importlib
import json
import sqlite3

from openpyxl import Workbook


def unknown_products():
    return [
        {
            "reference": "ZZ100-1",
            "description": "Custom modular device 2P 16A",
            "price": 10.0,
        },
        {
            "reference": "ZZ100-2",
            "description": "Custom modular device 2P 20A",
            "price": 11.0,
        },
    ]


def test_import_ai_classifies_an_unresolved_family_group_once_and_reuses_cache(
    monkeypatch,
):
    importer = importlib.import_module("Codes.import")
    calls = []

    def fake_gemini(system, user):
        calls.append((system, user))
        return json.dumps({
            "groups": [{
                "group_key": "ZZ100",
                "family": "ZZ_MODULAR",
                "category": "MCB",
                "product_kind": "standalone",
                "confidence": 0.94,
                "applies_to_all": True,
            }]
        })

    monkeypatch.setattr(importer, "gemini", fake_gemini)
    connection = sqlite3.connect(":memory:")
    try:
        first = unknown_products()
        first_summary = importer.classify_catalog_products(connection, "TESTCO", first)
        assert first_summary["groups"] == 1
        assert first_summary["ai"] == 2
        assert len(calls) == 1
        assert all(row["spec"].family == "ZZ_MODULAR" for row in first)

        monkeypatch.setattr(
            importer, "gemini",
            lambda *args: (_ for _ in ()).throw(AssertionError("cache was not reused")),
        )
        second = unknown_products()
        second_summary = importer.classify_catalog_products(connection, "TESTCO", second)
        assert second_summary["ai"] == 2
        assert all(row["spec"].source == "ai_family" for row in second)
    finally:
        connection.close()


def test_column_rules_do_not_mix_title_and_header_rows():
    importer = importlib.import_module("Codes.import")
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["PPL 2026 H1 - SCHNEIDER"])
    sheet.append(["REFERENCE", "DESCRIPTION", "PPL 2026 (EURO)"])
    sheet.append(["A9F74116", "MCB 1P 16A", 10.0])

    mapping, header_row = importer.map_columns_rules(sheet)

    assert header_row == 2
    assert mapping["ref"] == 0
    assert mapping["desc"] == 1
    assert mapping["price"] == 2
