import sqlite3

from app.catalog import classify_product, classify_products_in_database, ensure_catalog_schema


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


def test_phoenix_spd_reference_encodes_phase_plus_neutral_poles():
    single = classify_product(
        "PHOENIX CONTACT", "PHC-VAL-MS 230/1+1",
        "Class C surge arrester for a 1-phase network",
    )
    three_phase = classify_product(
        "PHOENIX CONTACT", "PHC-VAL-MS-EE-T2-3+1-385-FM",
        "Type 2 surge arrester for a 3-phase network",
    )

    assert single.poles == "1PN"
    assert single.spd_type == "TYPE 2"
    assert three_phase.poles == "3PN"


def test_eaton_power_meter_and_control_families_are_classified_locally():
    meter = classify_product(
        "EATON", "EAT-F500", "Power Quality Analyser with integrated Modbus"
    )
    pilot = classify_product(
        "EATON", "A22-LCLED220-R", "Ind.light compact,red,LED 220V AC/DC"
    )
    selector = classify_product(
        "EATON", "C22S-WK3-K20", "Selector sw. actuat. w.thumb-grip,3p,2NO"
    )
    accessory = classify_product(
        "EATON", "EAT-A5WIFI", "Modbus WIFI module for RI-F500 and RI-F550"
    )

    assert (meter.family, meter.category, meter.product_kind) == (
        "METER", "METER", "standalone"
    )
    assert (pilot.family, pilot.category) == ("A22_PILOT_LAMP", "PILOT_LAMP")
    assert (selector.family, selector.category) == ("CONTROL_SELECTOR", "SELECTOR_SWITCH")
    assert (accessory.family, accessory.category, accessory.product_kind) == (
        "EATON_METER_ACCESSORY", "ACCESSORY", "accessory"
    )


def test_current_transformer_is_classified_locally():
    result = classify_product(
        "MBS", "MBS-ASK 31.3/400", "Low-voltage current transformer; 400/5A 5VA Cl.1"
    )

    assert (result.family, result.category, result.product_kind) == (
        "CT", "CT", "standalone"
    )


def test_phoenix_complete_spd_is_not_classified_as_power_supply():
    result = classify_product(
        "PHOENIX CONTACT",
        "PHC-SYS-SET/3",
        "Plug-in lightning arrester for 3-phase power supply networks",
    )

    assert (result.family, result.category, result.product_kind) == (
        "PHOENIX_SPD", "SPD", "standalone"
    )
    assert result.poles == "3P"


def test_phoenix_replacement_spd_plug_remains_an_accessory():
    result = classify_product(
        "PHOENIX CONTACT",
        "PHC-FLT-SEC-T1-350/25-P",
        "Replacement plug for lightning and surge arresters",
    )

    assert (result.family, result.category, result.product_kind) == (
        "PHOENIX_SPD_ACCESSORY", "ACCESSORY", "accessory"
    )


def test_phoenix_spd_base_element_is_not_a_complete_arrester():
    result = classify_product(
        "PHOENIX CONTACT",
        "PHC-SP-EE-VAL-T2-BE-1+1-UT-R",
        "Base element for type 2 surge protective device",
    )

    assert (result.family, result.category, result.product_kind) == (
        "PHOENIX_SPD_ACCESSORY", "ACCESSORY", "accessory"
    )


def test_only_unclassified_preserves_ai_grouped_other_products(tmp_path):
    database = tmp_path / "pricing.db"
    with legacy_database(database) as connection:
        ensure_catalog_schema(connection)
        connection.execute(
            """
            INSERT INTO product_families(
                manufacturer, family_code, category, product_kind, description
            ) VALUES('SCHNEIDER', 'AI_OTHER', 'OTHER', 'other', 'AI family')
            """
        )
        family_id = connection.execute(
            "SELECT id FROM product_families WHERE family_code='AI_OTHER'"
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE products SET family_id=?, category='OTHER', product_kind='other',
                classification_source='ai_family', classification_confidence=0.9
            WHERE id=1
            """,
            (family_id,),
        )

        updated = classify_products_in_database(connection, only_unclassified=True)
        preserved = connection.execute(
            """
            SELECT family_id, category, classification_source
            FROM products WHERE id=1
            """
        ).fetchone()

    assert updated == 3
    assert preserved == (family_id, "OTHER", "ai_family")


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
