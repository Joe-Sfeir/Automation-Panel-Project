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


def component_description(component: Component) -> str:
    values = [
        component.category, component.poles, component.amps, component.curve,
        component.breaking_capacity, component.sensitivity, component.spd_type,
    ]
    return " ".join(str(value) for value in values if value and str(value) != "-")


class PricingService:
    def __init__(self, database: str | Path, progress=None):
        self.database = Path(database)
        self.progress = progress or (lambda message: None)

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
                for component in panel.components:
                    if component.review_status == "excluded":
                        continue
                    manufacturer = (component.manufacturer or project.pricing.default_manufacturer).upper()
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
        for row in rows:
            product_tokens = self._normalize_tokens(f"{row['reference']} {row['description']}", aliases)
            overlap = query_tokens.intersection(product_tokens)
            if not overlap:
                continue
            recall = len(overlap) / max(1, len(query_tokens))
            precision = len(overlap) / max(1, min(len(product_tokens), len(query_tokens) * 2))
            score = 0.8 * recall + 0.2 * precision
            candidates.append(CandidateMatch(
                str(row["reference"]), str(row["description"]), float(row["price"]),
                currency, round(score, 4),
            ))
        candidates.sort(key=lambda item: (-item.score, item.price, item.reference))
        top = candidates[:6]
        if not top or top[0].score < 0.35:
            return MatchResult(
                component.id, manufacturer, edition_name, alternatives=top[:5],
                quantity=component.quantity, currency=currency, tier="NF",
                review_status="unmatched", note="No sufficiently similar catalog product",
            )
        chosen = top[0]
        tier = "T1" if chosen.score >= 0.75 else "T2"
        status = "auto_matched" if tier == "T1" else "needs_review"
        return MatchResult(
            component.id, manufacturer, edition_name,
            chosen_reference=chosen.reference, chosen_description=chosen.description,
            alternatives=top[1:6], score=chosen.score, tier=tier,
            unit_price=chosen.price, quantity=component.quantity,
            total_price=round(chosen.price * component.quantity, 2), currency=currency,
            review_status=status,
            note="Deterministic token match" if tier == "T1" else "Candidate match requires review",
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
