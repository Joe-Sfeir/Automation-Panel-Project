from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .domain import CandidateMatch, Component, MatchResult, ProjectDocument
from .persistence import atomic_write_json
from .review import assert_ready_for_pricing


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", value.lower().replace("+", "n"))


def _poles(value: str) -> str:
    text = str(value or "").upper()
    compact = re.sub(r"\s+", "", text)
    if re.search(r"1P(?:\+?N)|1PN", compact):
        return "1PN"
    if re.search(r"3P(?:\+?N)|3PN", compact):
        return "3PN"
    match = re.search(r"(?:^|[^A-Z0-9])([1-4])P(?:$|[^A-Z0-9+])", text)
    return f"{match.group(1)}P" if match else "-"


def _current(value: str) -> float | None:
    values = [
        float(match.group(1))
        for match in re.finditer(r"(?<![A-Z0-9])([0-9]+(?:\.[0-9]+)?)\s*A\b", str(value or "").upper())
        if float(match.group(1)) <= 2500
    ]
    return values[-1] if values else None


def _sensitivity(value: str) -> float | None:
    match = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*MA\b", str(value or "").upper())
    return float(match.group(1)) if match else None


def _curve(value: str) -> str:
    text = str(value or "").upper()
    if text.strip() in {"B", "C", "D"}:
        return text.strip()
    match = re.search(r"(?:\bCURVE\s*|\bCOURBE\s*|\b1P(?:N)?\s+|\b[234]P\s+)([BCD])\b", text)
    return match.group(1) if match else "-"


def _category(value: str) -> str:
    text = str(value or "").upper()
    if any(token in text for token in (
        "HANDLE", "INTERLOCK", "AUXILIARY", "AUX CONTACT", "SHUNT",
        "UNDERVOLTAGE", "COIL", "COVER", "TERMINAL", "SEALING",
    )):
        return "ACCESSORY"
    for category, patterns in (
        ("RCBO", ("RCBO", "IDPNA VIGI")),
        ("RCCB", ("RCCB", "IID ", "RCD")),
        ("MCCB", ("MCCB", "NSX", "COMPACT")),
        ("MCB", ("MCB", "IC60", "IC40", "EASY9")),
        ("SWITCH", ("SWITCH", "ISOLATOR", "DISCONNECTOR", "INS")),
    ):
        if any(pattern in text for pattern in patterns):
            return category
    return "-"


def _structured_compatibility(component: Component, description: str) -> tuple[bool, float, list[str]]:
    """Apply electrical constraints before fuzzy catalog ranking.

    Returns compatibility, a small ranking bonus, and explicit substitutions.
    Unknown catalog fields never disqualify a row, but contradictory stated
    fields do. Pricing never raises or lowers an observed rating: if the exact
    stated value is unavailable, the result stays unmatched for manual choice.
    """
    bonus = 0.0
    substitutions = []
    product_category = _category(description)
    if product_category != "-":
        if product_category != component.category:
            return False, 0.0, []
        bonus += 0.08
    required_poles = _poles(component.poles)
    product_poles = _poles(description)
    if required_poles != "-" and product_poles != "-":
        if required_poles != product_poles:
            return False, 0.0, []
        bonus += 0.06
    required_current = _current(component.amps)
    product_current = _current(description)
    if required_current is not None and product_current is not None:
        if product_current != required_current:
            return False, 0.0, []
        bonus += 0.08
    required_sensitivity = _sensitivity(component.sensitivity)
    product_sensitivity = _sensitivity(description)
    if required_sensitivity is not None and product_sensitivity is not None:
        if product_sensitivity != required_sensitivity:
            return False, 0.0, []
        bonus += 0.08
    required_curve = _curve(component.curve)
    product_curve = _curve(description)
    if required_curve != "-" and product_curve != "-":
        if required_curve != product_curve:
            return False, 0.0, []
        bonus += 0.05
    return True, bonus, substitutions


def component_description(component: Component) -> str:
    values = [
        component.category, component.poles, component.amps, component.curve,
        component.breaking_capacity, component.sensitivity, component.spd_type,
    ]
    return " ".join(str(value) for value in values if value and str(value) != "-")


def effective_component_manufacturer(project: ProjectDocument, component: Component) -> str:
    """Resolve the pricing brand without allowing stale extraction metadata to leak in."""
    selected = [str(name).strip().upper() for name in project.pricing.editions if str(name).strip()]
    if len(selected) <= 1:
        return selected[0] if selected else project.pricing.default_manufacturer.strip().upper()
    return (component.manufacturer or project.pricing.default_manufacturer).strip().upper()


class PricingService:
    def __init__(self, database: str | Path, progress=None, should_cancel=None):
        self.database = Path(database)
        self.progress = progress or (lambda message: None)
        self.should_cancel = should_cancel or (lambda: False)

    def _check_cancelled(self) -> None:
        if self.should_cancel():
            raise InterruptedError("Pricing cancelled")

    def validate_database(self) -> dict[str, int]:
        if not self.database.exists():
            raise FileNotFoundError(f"Pricing database not found: {self.database}")
        with sqlite3.connect(self.database) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Pricing database integrity check failed: {integrity}")
            counts = {}
            for table in ("manufacturers", "ppl_editions", "products"):
                counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            return counts

    def price_project(self, project: ProjectDocument, output_path: str | Path) -> dict:
        self._check_cancelled()
        assert_ready_for_pricing(project)
        self.validate_database()
        results: list[MatchResult] = []
        product_cache: dict[int, list[sqlite3.Row]] = {}
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            aliases = {
                str(row["alias"]).lower(): str(row["canonical"]).lower()
                for row in connection.execute("SELECT alias, canonical FROM term_aliases")
            }
            for panel in project.panels:
                self._check_cancelled()
                for component in panel.components:
                    self._check_cancelled()
                    if component.review_status == "excluded":
                        continue
                    manufacturer = effective_component_manufacturer(project, component)
                    edition_config = project.pricing.editions.get(manufacturer, {})
                    year = int(edition_config.get("year", datetime.now().year))
                    half = str(edition_config.get("half", "H1")).upper()
                    self.progress(f"Matching {panel.reference}: {component_description(component)} [{manufacturer}]")
                    results.append(self._match(
                        connection, aliases, component, manufacturer, year, half, product_cache
                    ))
        project.match_results = results
        payload = self._pricing_payload(project, results)
        atomic_write_json(output_path, payload)
        return payload

    def _edition(self, connection, manufacturer: str, year: int, half: str):
        return connection.execute(
            """
            SELECT pe.id, pe.currency FROM ppl_editions pe
            JOIN manufacturers m ON m.id = pe.manufacturer_id
            WHERE UPPER(m.name) = ? AND pe.year = ? AND pe.half = ?
            """,
            (manufacturer.upper(), year, half),
        ).fetchone()

    def _match(self, connection, aliases, component, manufacturer, year, half, product_cache) -> MatchResult:
        edition = self._edition(connection, manufacturer, year, half)
        edition_name = f"{manufacturer} {year} {half}"
        if not edition:
            return MatchResult(
                component.id, manufacturer, edition_name, quantity=component.quantity,
                tier="NF", review_status="unmatched", note="Price-list edition is not installed",
            )
        edition_id, currency = int(edition["id"]), str(edition["currency"])
        if component.forced_reference:
            row = connection.execute(
                """
                SELECT reference, description, price FROM products
                WHERE ppl_edition_id = ? AND
                      (reference = ? COLLATE NOCASE OR order_number = ? COLLATE NOCASE)
                """,
                (edition_id, component.forced_reference, component.forced_reference),
            ).fetchone()
            if not row:
                return MatchResult(
                    component.id, manufacturer, edition_name, quantity=component.quantity,
                    currency=currency, tier="NF", review_status="unmatched",
                    note=f"Forced reference not found: {component.forced_reference}",
                )
            price = float(row["price"])
            return MatchResult(
                component.id, manufacturer, edition_name,
                chosen_reference=str(row["reference"]), chosen_description=str(row["description"]),
                score=1.0, tier="FORCED", unit_price=price, quantity=component.quantity,
                total_price=round(price * component.quantity, 2), currency=currency,
                review_status="confirmed", note="Manually forced catalog reference",
            )

        query_tokens = self._normalize_tokens(component_description(component), aliases)
        rows = product_cache.get(edition_id)
        if rows is None:
            rows = connection.execute(
                "SELECT reference, description, price FROM products WHERE ppl_edition_id = ?",
                (edition_id,),
            ).fetchall()
            product_cache[edition_id] = rows
        candidates = []
        substitutions_by_reference: dict[str, list[str]] = {}
        for row_index, row in enumerate(rows):
            if row_index % 250 == 0:
                self._check_cancelled()
            compatible, structured_bonus, substitutions = _structured_compatibility(
                component, str(row["description"])
            )
            if not compatible:
                continue
            product_tokens = self._normalize_tokens(f"{row['reference']} {row['description']}", aliases)
            overlap = query_tokens.intersection(product_tokens)
            if not overlap:
                continue
            recall = len(overlap) / max(1, len(query_tokens))
            precision = len(overlap) / max(1, min(len(product_tokens), len(query_tokens) * 2))
            score = min(1.0, 0.8 * recall + 0.2 * precision + structured_bonus)
            reference = str(row["reference"])
            candidates.append(CandidateMatch(
                reference, str(row["description"]), float(row["price"]),
                currency, round(score, 4),
            ))
            substitutions_by_reference[reference] = substitutions
        candidates.sort(key=lambda item: (-item.score, item.price, item.reference))
        top = candidates[:6]
        if not top or top[0].score < 0.35:
            return MatchResult(
                component.id, manufacturer, edition_name, alternatives=top[:5],
                quantity=component.quantity, currency=currency, tier="NF",
                review_status="unmatched", note="No sufficiently similar catalog product",
            )
        chosen = top[0]
        ambiguous = len(top) > 1 and top[1].score >= chosen.score - 0.02
        substitutions = substitutions_by_reference.get(chosen.reference, [])
        tier = "T1" if chosen.score >= 0.75 and not ambiguous and not substitutions else "T2"
        status = "auto_matched" if tier == "T1" else "check_candidate"
        if ambiguous:
            note = "Multiple electrically compatible catalog families have equivalent scores"
        elif substitutions:
            note = "Compatible commercial substitution requires review: " + "; ".join(substitutions)
        else:
            note = "Deterministic structured match" if tier == "T1" else "Candidate match requires review"
        return MatchResult(
            component.id, manufacturer, edition_name,
            chosen_reference=chosen.reference, chosen_description=chosen.description,
            alternatives=top[1:6], score=chosen.score, tier=tier,
            unit_price=chosen.price, quantity=component.quantity,
            total_price=round(chosen.price * component.quantity, 2), currency=currency,
            review_status=status,
            note=note,
        )

    @staticmethod
    def _normalize_tokens(value: str, aliases: dict[str, str]) -> set[str]:
        result = set()
        for token in _tokens(value):
            canonical = aliases.get(token, token)
            result.update(_tokens(canonical))
        return result

    @staticmethod
    def _pricing_payload(project: ProjectDocument, results: list[MatchResult]) -> dict:
        by_component = {item.component_id: item for item in results}
        panels = []
        currencies = set()
        grand_ht = 0.0
        for panel in project.panels:
            items = []
            total = 0.0
            for component in panel.components:
                if component.review_status == "excluded":
                    continue
                result = by_component.get(component.id)
                if not result:
                    continue
                currencies.add(result.currency)
                row = asdict(result)
                row["description"] = component_description(component)
                row["panel_id"] = panel.id
                items.append(row)
                total += result.total_price
            panels.append({"panel_id": panel.id, "panel_reference": panel.reference, "ht": round(total, 2), "items": items})
            grand_ht += total
        tva = round(grand_ht * project.pricing.tva_rate, 2)
        return {
            "schema_version": 1,
            "project_id": project.id,
            "project_reference": project.reference,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "tva_rate": project.pricing.tva_rate,
            "currencies": sorted(value for value in currencies if value),
            "grand_ht": round(grand_ht, 2),
            "tva": tva,
            "grand_ttc": round(grand_ht + tva, 2),
            "panels": panels,
        }
