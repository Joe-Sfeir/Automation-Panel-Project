import sqlite3

from app.catalog import classify_products_in_database, ensure_catalog_schema


def legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE ppl_editions (
            id INTEGER PRIMARY KEY, manufacturer_id INTEGER, year INTEGER,
            half TEXT, currency TEXT
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY, ppl_edition_id INTEGER, reference TEXT,
            order_number TEXT, description TEXT, price REAL
        );
        INSERT INTO manufacturers VALUES (1, 'SCHNEIDER');
        INSERT INTO ppl_editions VALUES (10, 1, 2026, 'H1', 'EUR');
        INSERT INTO products VALUES
            (1, 10, 'A9R41425', NULL, 'ACTI9 IID 4P 25A 30MA AC RCCB', 99.87),
            (2, 10, 'A9D34610', NULL, 'IDPNA VIGI 10A C 30MA AC RESIDUAL', 91.17),
            (3, 10, 'A9V41425', NULL, 'ACTI9 VIGI IC60 4P 25A 30MA ADD-ON', 133.13),
            (4, 10, 'A9N18361', NULL, 'NG125N C 4P 10A MCB', 80.00);
    """)
    connection.commit()
    return connection


def test_legacy_database_is_migrated_and_product_families_are_persisted(tmp_path):
    database = tmp_path / "pricing.db"
    with legacy_database(database) as connection:
        ensure_catalog_schema(connection)
        updated = classify_products_in_database(connection)
        connection.commit()

        columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                """
                SELECT p.reference, p.category, p.product_kind, pf.family_code,
                       p.poles, p.rated_current_a, p.sensitivity_ma
                FROM products p
                LEFT JOIN product_families pf ON pf.id=p.family_id
                """
            )
        }

    assert updated == 4
    assert {
        "family_id", "category", "product_kind", "poles", "rated_current_a",
        "sensitivity_ma", "curve", "breaking_capacity_ka",
        "classification_source", "classification_confidence",
    }.issubset(columns)
    assert rows["A9R41425"][:3] == ("RCCB", "standalone", "IID")
    assert rows["A9D34610"][:3] == ("RCBO", "standalone", "IDPN_VIGI")
    assert rows["A9V41425"][:3] == ("RCCB", "residual_add_on", "VIGI")
    assert rows["A9N18361"][:3] == ("MCB", "standalone", "NG125")
    assert rows["A9R41425"][3:] == ("4P", 25.0, 30.0)


def test_current_schema_builds_from_scratch(tmp_path):
    connection = sqlite3.connect(":memory:")
    try:
        from pathlib import Path

        sql = (Path(__file__).resolve().parents[1] / "Codes" / "schema.sql").read_text(
            encoding="utf-8"
        )
        connection.executescript(sql)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()

    assert {"products", "product_families", "family_patterns", "ai_match_cache"}.issubset(
        tables
    )
