from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence


CATALOG_CATEGORIES = {
    "MCB", "MCCB", "RCCB", "RCBO", "SWITCH", "CONTACTOR", "SPD",
    "FUSE_HOLDER", "PILOT_LAMP", "METER", "CT", "RELAY",
    "SELECTOR_SWITCH", "TIMER", "PLC", "POWER_SUPPLY", "ACCESSORY", "OTHER",
}
PRODUCT_KINDS = {"standalone", "residual_add_on", "accessory", "other"}


@dataclass(frozen=True)
class FamilyRule:
    manufacturer: str
    family_code: str
    category: str
    product_kind: str
    pattern: str
    priority: int = 100
    description: str = ""


@dataclass(frozen=True)
class ProductSpec:
    family: str = ""
    category: str = "OTHER"
    product_kind: str = "other"
    poles: str = "-"
    rated_current_a: float | None = None
    sensitivity_ma: float | None = None
    curve: str = "-"
    breaking_capacity_ka: float | None = None
    source: str = "unclassified"
    confidence: float = 0.0


# These are bootstrap rules, not a second runtime authority. ensure_catalog_schema()
# persists them in pricing.db, where they can be inspected and extended without
# changing matcher code. Higher priority wins.
DEFAULT_FAMILY_RULES: tuple[FamilyRule, ...] = (
    FamilyRule("*", "ACCESSORY", "ACCESSORY", "accessory", r"\b(HANDLE|PADLOCK|AUXILIARY|AUX CONTACT|SHUNT|UNDERVOLTAGE|COIL|COVER|TERMINAL|SEALING|SPACER|PLATINE|FRONT PL|M\.PL)\b", 1000, "Commercial accessory"),
    FamilyRule("SCHNEIDER", "IDPN_VIGI", "RCBO", "standalone", r"\b(IDPNA?\s+VIGI|IDPN\s+N\s+VIGI|ICV40)\b", 950, "Acti9 combined residual/overcurrent breaker"),
    FamilyRule("SCHNEIDER", "VIGI", "RCCB", "residual_add_on", r"\bVIGI\b", 900, "Residual-current add-on module"),
    FamilyRule("SCHNEIDER", "IID", "RCCB", "standalone", r"\b(IID(?:40)?|RCCB[- ]?ID)\b", 850, "Acti9 residual-current circuit breaker"),
    FamilyRule("SCHNEIDER", "COMPACT_NSX", "MCCB", "standalone", r"\b(COMPACT\s+)?NSX\d*\b", 840, "Compact NSX molded-case circuit breaker"),
    FamilyRule("SCHNEIDER", "NG125", "MCB", "standalone", r"\bNG125[NLH]?\b", 830, "Acti9 NG125 miniature circuit breaker"),
    FamilyRule("SCHNEIDER", "IC60", "MCB", "standalone", r"\bIC60[NLH]?\b", 820, "Acti9 iC60 miniature circuit breaker"),
    FamilyRule("SCHNEIDER", "IK60", "MCB", "standalone", r"\bIK60[NLH]?\b", 820, "Acti9 iK60 miniature circuit breaker"),
    FamilyRule("SCHNEIDER", "IC40", "MCB", "standalone", r"\bIC40[NLH]?\b", 815, "Acti9 iC40 miniature circuit breaker"),
    FamilyRule("SCHNEIDER", "EASY9", "MCB", "standalone", r"\bEASY9\b.*\bMCB\b|\bEZ9F", 810, "Easy9 miniature circuit breaker"),
    FamilyRule("SCHNEIDER", "C120", "MCB", "standalone", r"\bC120[NLH]?\b", 805, "Acti9 C120 miniature circuit breaker"),
    FamilyRule("SCHNEIDER", "ISW", "SWITCH", "standalone", r"\bISW\d*\b", 800, "Acti9 switch disconnector"),
    FamilyRule("SCHNEIDER", "INS", "SWITCH", "standalone", r"\bINS\d+\b", 790, "Compact switch disconnector"),
    FamilyRule("*", "RCBO", "RCBO", "standalone", r"\bRCBO\b", 700, "Residual-current breaker with overcurrent protection"),
    FamilyRule("*", "RCCB", "RCCB", "standalone", r"\b(RCCB|RCD)\b", 690, "Residual-current circuit breaker"),
    FamilyRule("*", "MCCB", "MCCB", "standalone", r"\bMCCB\b", 680, "Molded-case circuit breaker"),
    FamilyRule("*", "MCB", "MCB", "standalone", r"\bMCB\b|MINIATURE\s+CIRCUIT\s+BREAKER", 670, "Miniature circuit breaker"),
    FamilyRule("*", "SWITCH", "SWITCH", "standalone", r"\b(SWITCH|ISOLATOR|DISCONNECTOR)\b", 660, "Switch or disconnector"),
    FamilyRule("*", "CONTACTOR", "CONTACTOR", "standalone", r"\bCONTACTOR\b", 650, "Contactor"),
    FamilyRule("*", "SPD", "SPD", "standalone", r"\b(SPD|SURGE PROTECT)\b", 640, "Surge-protection device"),
    FamilyRule("*", "FUSE_HOLDER", "FUSE_HOLDER", "standalone", r"\b(FUSE HOLDER|FUSE-HOLDER)\b", 630, "Fuse holder"),
    FamilyRule("*", "PILOT_LAMP", "PILOT_LAMP", "standalone", r"\b(PILOT LAMP|INDICATOR LAMP)\b", 620, "Pilot lamp"),
    FamilyRule("*", "POWER_SUPPLY", "POWER_SUPPLY", "standalone", r"\bPOWER SUPP(?:LY)?\b", 610, "Power supply"),
    FamilyRule("*", "SELECTOR_SWITCH", "SELECTOR_SWITCH", "standalone", r"\bSELECTOR SWITCH\b", 600, "Selector switch"),
    FamilyRule("*", "METER", "METER", "standalone", r"\b(POWER METER|ENERGY METER|MULTIMETER)\b", 590, "Electrical meter"),
    FamilyRule("*", "RELAY", "RELAY", "standalone", r"\bRELAY\b", 580, "Relay"),
)


PRODUCT_SPEC_COLUMNS: dict[str, str] = {
    "family_id": "INTEGER",
    "category": "TEXT",
    "product_kind": "TEXT",
    "poles": "TEXT",
    "rated_current_a": "REAL",
    "sensitivity_ma": "REAL",
    "curve": "TEXT",
    "breaking_capacity_ka": "REAL",
    "classification_source": "TEXT",
    "classification_confidence": "REAL",
}


def ensure_catalog_schema(connection: sqlite3.Connection) -> None:
    """Migrate a legacy pricing DB in-place without deleting catalog data."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS product_families (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manufacturer TEXT NOT NULL,
            family_code TEXT NOT NULL,
            category TEXT NOT NULL,
            product_kind TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            UNIQUE(manufacturer, family_code)
        );
        CREATE TABLE IF NOT EXISTS family_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            UNIQUE(family_id, pattern),
            FOREIGN KEY (family_id) REFERENCES product_families(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS catalog_ai_cache (
            cache_kind TEXT NOT NULL,
            manufacturer TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            response_json TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(cache_kind, manufacturer, cache_key)
        );
        CREATE TABLE IF NOT EXISTS ai_match_cache (
            cache_key TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "products" in tables:
        existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(products)")}
        for name, sql_type in PRODUCT_SPEC_COLUMNS.items():
            if name not in existing:
                connection.execute(f'ALTER TABLE products ADD COLUMN "{name}" {sql_type}')
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_structured "
            "ON products(ppl_edition_id, category, product_kind, poles, rated_current_a, sensitivity_ma)"
        )
    _seed_family_rules(connection)


def _seed_family_rules(connection: sqlite3.Connection) -> None:
    for rule in DEFAULT_FAMILY_RULES:
        connection.execute(
            """
            INSERT INTO product_families(manufacturer, family_code, category, product_kind, description)
            VALUES(?,?,?,?,?)
            ON CONFLICT(manufacturer, family_code) DO UPDATE SET
                category=excluded.category,
                product_kind=excluded.product_kind,
                description=excluded.description
            """,
            (rule.manufacturer, rule.family_code, rule.category, rule.product_kind, rule.description),
        )
        family_id = int(connection.execute(
            "SELECT id FROM product_families WHERE manufacturer=? AND family_code=?",
            (rule.manufacturer, rule.family_code),
        ).fetchone()[0])
        connection.execute(
            """
            INSERT INTO family_patterns(family_id, pattern, priority)
            VALUES(?,?,?)
            ON CONFLICT(family_id, pattern) DO UPDATE SET priority=excluded.priority
            """,
            (family_id, rule.pattern, rule.priority),
        )


def load_family_rules(connection: sqlite3.Connection) -> list[FamilyRule]:
    ensure_catalog_schema(connection)
    rows = connection.execute(
        """
        SELECT pf.manufacturer, pf.family_code, pf.category, pf.product_kind,
               fp.pattern, fp.priority, pf.description
        FROM family_patterns fp
        JOIN product_families pf ON pf.id = fp.family_id
        ORDER BY fp.priority DESC, pf.manufacturer DESC, pf.family_code
        """
    ).fetchall()
    return [FamilyRule(*row) for row in rows]


def family_id_for(
    connection: sqlite3.Connection, manufacturer: str, family_code: str
) -> int | None:
    if not family_code:
        return None
    row = connection.execute(
        """
        SELECT id FROM product_families
        WHERE family_code=? AND manufacturer IN (?, '*')
        ORDER BY CASE WHEN manufacturer=? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (family_code, manufacturer.upper(), manufacturer.upper()),
    ).fetchone()
    return int(row[0]) if row else None


def _poles(value: str) -> str:
    text = str(value or "").upper()
    compact = re.sub(r"\s+", "", text)
    if re.search(r"1P(?:\+?N)|1PN", compact):
        return "1PN"
    if re.search(r"3P(?:\+?N)|3PN", compact):
        return "3PN"
    match = re.search(r"(?:^|[^A-Z0-9])([1-4])P(?:$|[^A-Z0-9+])", text)
    return f"{match.group(1)}P" if match else "-"


def _rated_current(value: str) -> float | None:
    text = str(value or "").upper()
    values = []
    for match in re.finditer(r"(?<![A-Z0-9])([0-9]+(?:\.[0-9]+)?)\s*A\b", text):
        current = float(match.group(1))
        if current <= 2500:
            values.append(current)
    return values[0] if values else None


def _sensitivity(value: str) -> float | None:
    match = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*MA\b", str(value or "").upper())
    return float(match.group(1)) if match else None


def _curve(value: str) -> str:
    text = str(value or "").upper()
    match = re.search(
        r"(?:\bCURVE\s*|\bCOURBE\s*|\b[1-4]P(?:N)?\s+|\bMCB\s+)([BCD])\b", text
    )
    return match.group(1) if match else "-"


def _breaking_capacity(value: str) -> float | None:
    text = str(value or "").upper()
    match = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*KA\b", text)
    if match:
        return float(match.group(1))
    match = re.search(r"\b(3000|4500|6000|10000|15000|25000|36000)\s*A\b", text)
    return float(match.group(1)) / 1000.0 if match else None


def classify_product(
    manufacturer: str,
    reference: str,
    description: str,
    rules: Sequence[FamilyRule] | None = None,
) -> ProductSpec:
    manufacturer = str(manufacturer or "").upper().strip()
    text = f"{reference or ''} {description or ''}".upper()
    selected: FamilyRule | None = None
    for rule in sorted(rules or DEFAULT_FAMILY_RULES, key=lambda item: item.priority, reverse=True):
        if rule.manufacturer not in {"*", manufacturer}:
            continue
        if re.search(rule.pattern, text, re.I):
            selected = rule
            break
    if selected:
        family = selected.family_code
        category = selected.category if selected.category in CATALOG_CATEGORIES else "OTHER"
        product_kind = selected.product_kind if selected.product_kind in PRODUCT_KINDS else "other"
        source = "family_rule"
        confidence = 0.97
    else:
        family = ""
        category = "OTHER"
        product_kind = "other"
        source = "unclassified"
        confidence = 0.0
    return ProductSpec(
        family=family,
        category=category,
        product_kind=product_kind,
        poles=_poles(text),
        rated_current_a=_rated_current(text),
        sensitivity_ma=_sensitivity(text),
        curve=_curve(text),
        breaking_capacity_ka=_breaking_capacity(text),
        source=source,
        confidence=confidence,
    )


def merge_ai_family(spec: ProductSpec, ai_row: dict) -> ProductSpec:
    category = str(ai_row.get("category", "OTHER")).upper()
    product_kind = str(ai_row.get("product_kind", "other")).lower()
    confidence = max(0.0, min(1.0, float(ai_row.get("confidence", 0.0) or 0.0)))
    if category not in CATALOG_CATEGORIES or product_kind not in PRODUCT_KINDS:
        return spec
    return ProductSpec(
        family=str(ai_row.get("family", "") or "").upper().strip(),
        category=category,
        product_kind=product_kind,
        poles=spec.poles,
        rated_current_a=spec.rated_current_a,
        sensitivity_ma=spec.sensitivity_ma,
        curve=spec.curve,
        breaking_capacity_ka=spec.breaking_capacity_ka,
        source="ai_family",
        confidence=confidence,
    )


def spec_database_values(
    connection: sqlite3.Connection, manufacturer: str, spec: ProductSpec
) -> tuple:
    family_id = family_id_for(connection, manufacturer, spec.family)
    if family_id is None and spec.family:
        connection.execute(
            """
            INSERT INTO product_families(
                manufacturer, family_code, category, product_kind, description
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(manufacturer, family_code) DO UPDATE SET
                category=excluded.category,
                product_kind=excluded.product_kind
            """,
            (
                manufacturer.upper(), spec.family, spec.category,
                spec.product_kind, "AI-classified catalog family",
            ),
        )
        family_id = family_id_for(connection, manufacturer, spec.family)
    return (
        family_id,
        spec.category,
        spec.product_kind,
        spec.poles,
        spec.rated_current_a,
        spec.sensitivity_ma,
        spec.curve,
        spec.breaking_capacity_ka,
        spec.source,
        spec.confidence,
    )


def classify_products_in_database(
    connection: sqlite3.Connection,
    *,
    edition_id: int | None = None,
    only_unclassified: bool = True,
) -> int:
    ensure_catalog_schema(connection)
    rules = load_family_rules(connection)
    where = []
    params: list[object] = []
    if edition_id is not None:
        where.append("p.ppl_edition_id=?")
        params.append(int(edition_id))
    if only_unclassified:
        where.append(
            "(COALESCE(p.classification_source, '')='' "
            "OR COALESCE(p.category, '') IN ('', 'OTHER'))"
        )
    sql = """
        SELECT p.id, p.reference, p.description, m.name
        FROM products p
        JOIN ppl_editions pe ON pe.id=p.ppl_edition_id
        JOIN manufacturers m ON m.id=pe.manufacturer_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    updated = 0
    for product_id, reference, description, manufacturer in connection.execute(sql, params):
        spec = classify_product(manufacturer, reference, description, rules)
        connection.execute(
            """
            UPDATE products SET
                family_id=?, category=?, product_kind=?, poles=?, rated_current_a=?,
                sensitivity_ma=?, curve=?, breaking_capacity_ka=?,
                classification_source=?, classification_confidence=?
            WHERE id=?
            """,
            (*spec_database_values(connection, manufacturer, spec), int(product_id)),
        )
        updated += 1
    return updated


def row_product_spec(row: sqlite3.Row, manufacturer: str = "") -> ProductSpec:
    keys = set(row.keys())
    category = str(row["category"] or "") if "category" in keys else ""
    if category:
        return ProductSpec(
            family=str(row["family_code"] or "") if "family_code" in keys else "",
            category=category,
            product_kind=str(row["product_kind"] or "other") if "product_kind" in keys else "other",
            poles=str(row["poles"] or "-") if "poles" in keys else "-",
            rated_current_a=float(row["rated_current_a"]) if "rated_current_a" in keys and row["rated_current_a"] is not None else None,
            sensitivity_ma=float(row["sensitivity_ma"]) if "sensitivity_ma" in keys and row["sensitivity_ma"] is not None else None,
            curve=str(row["curve"] or "-") if "curve" in keys else "-",
            breaking_capacity_ka=float(row["breaking_capacity_ka"]) if "breaking_capacity_ka" in keys and row["breaking_capacity_ka"] is not None else None,
            source=str(row["classification_source"] or "stored") if "classification_source" in keys else "stored",
            confidence=float(row["classification_confidence"] or 0.0) if "classification_confidence" in keys else 0.0,
        )
    return classify_product(
        manufacturer,
        str(row["reference"] if "reference" in keys else ""),
        str(row["description"] if "description" in keys else ""),
    )


def unresolved_family_groups(products: Iterable[dict]) -> dict[str, list[dict]]:
    """Group unresolved catalog rows so import-time AI reasons about families, not every SKU."""
    groups: dict[str, list[dict]] = {}
    for product in products:
        spec = product.get("spec")
        if isinstance(spec, ProductSpec) and spec.category != "OTHER":
            continue
        reference = str(product.get("reference", "")).upper()
        prefix_match = re.match(r"[A-Z]+[0-9]*[A-Z]*", reference)
        prefix = (prefix_match.group(0) if prefix_match else reference)[:6] or "UNKNOWN"
        groups.setdefault(prefix, []).append(product)
    return groups
