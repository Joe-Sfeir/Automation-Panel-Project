from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .catalog import (
    ProductSpec,
    classify_product,
    classify_products_in_database,
    ensure_catalog_schema,
    row_product_spec,
)
from .domain import CandidateMatch, Component, MatchResult, ProjectDocument
from .persistence import atomic_write_json
from .review import assert_ready_for_pricing
from .offer_defaults import canonical_manufacturer


STRUCTURED_CATEGORIES = {
    "MCB", "MCCB", "RCCB", "RCBO", "SWITCH", "CONTACTOR", "SPD",
    "FUSE_HOLDER", "PILOT_LAMP", "METER", "CT", "RELAY",
    "SELECTOR_SWITCH", "TIMER", "PLC", "POWER_SUPPLY",
}
RESIDUAL_CATEGORIES = {"RCCB", "RCBO"}
SAFE_CURRENT_UPSIZE = {"RCCB", "SWITCH", "CONTACTOR"}
CURVE_CATEGORIES = {"MCB", "RCBO"}
BREAKING_CAPACITY_CATEGORIES = {"MCB", "MCCB", "RCBO"}


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
    match = re.search(
        r"(?<![A-Z0-9])([0-9]+(?:\.[0-9]+)?)\s*A\b", str(value or "").upper()
    )
    return float(match.group(1)) if match else None


def _sensitivity(value: str) -> float | None:
    match = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*MA\b", str(value or "").upper())
    return float(match.group(1)) if match else None


def _curve(value: str) -> str:
    text = str(value or "").upper().strip()
    if text in {"B", "C", "D"}:
        return text
    match = re.search(r"\bCURVE\s*([BCD])\b", text)
    return match.group(1) if match else "-"


def _breaking_capacity(value: str) -> float | None:
    match = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*KA\b", str(value or "").upper())
    return float(match.group(1)) if match else None


def _spd_type(value: str) -> str:
    text = str(value or "").upper().replace("CLASS B+C", "TYPE 1+2")
    if re.search(r"\bTYPE\s*(?:I|1)\s*(?:\+|&|AND)\s*(?:II|2)\b", text):
        return "TYPE 1+2"
    match = re.search(r"\b(?:TYPE|CLASS|CATEGORY)\s*(I{1,3}|[123])\b", text)
    if not match:
        return "-"
    value = {"I": "1", "II": "2", "III": "3"}.get(match.group(1), match.group(1))
    return f"TYPE {value}"


def _category(value: str) -> str:
    return classify_product("", "", value).category


def _component_spec(component: Component) -> ProductSpec:
    category = str(component.category or "OTHER").upper()
    return ProductSpec(
        category=category,
        product_kind="standalone",
        poles=_poles(component.poles),
        rated_current_a=_current(component.amps),
        sensitivity_ma=_sensitivity(component.sensitivity),
        curve=_curve(component.curve),
        breaking_capacity_ka=_breaking_capacity(component.breaking_capacity),
        spd_type=_spd_type(component.spd_type),
        source="component",
        confidence=1.0,
    )


def _structured_compatibility(
    component: Component, description: str
) -> tuple[bool, float, list[str]]:
    """Compatibility shim retained for callers that inspect raw descriptions."""
    required = _component_spec(component)
    candidate = classify_product("", "", description)
    compatible, bonus, _ = _exact_compatibility(required, candidate)
    return compatible, bonus, []


def _exact_compatibility(
    required: ProductSpec, candidate: ProductSpec
) -> tuple[bool, float, list[str]]:
    notes: list[str] = []
    bonus = 0.0
    if required.category in STRUCTURED_CATEGORIES:
        if candidate.category != required.category or candidate.product_kind != "standalone":
            return False, 0.0, notes
        bonus += 0.12
    if required.poles != "-":
        pole_match = candidate.poles == required.poles
        if required.category == "SPD":
            pole_match = pole_match or (required.poles, candidate.poles) in {
                ("4P", "3PN"), ("3PN", "4P"),
                ("2P", "1PN"), ("1PN", "2P"),
            }
        if not pole_match:
            return False, 0.0, notes
        bonus += 0.08
    if required.rated_current_a is not None:
        if candidate.rated_current_a != required.rated_current_a:
            return False, 0.0, notes
        bonus += 0.10
    if required.category in RESIDUAL_CATEGORIES and required.sensitivity_ma is not None:
        if candidate.sensitivity_ma != required.sensitivity_ma:
            return False, 0.0, notes
        bonus += 0.10
    if required.category == "SPD" and required.spd_type != "-":
        if candidate.spd_type != required.spd_type:
            return False, 0.0, notes
        bonus += 0.10
    if required.category in CURVE_CATEGORIES and required.curve != "-":
        if candidate.curve != required.curve:
            return False, 0.0, notes
        bonus += 0.06
    if (
        required.category in BREAKING_CAPACITY_CATEGORIES
        and
        required.breaking_capacity_ka is not None
        and candidate.breaking_capacity_ka is not None
        and candidate.breaking_capacity_ka < required.breaking_capacity_ka
    ):
        return False, 0.0, notes
    return True, bonus, notes


def component_description(component: Component) -> str:
    values = [
        component.category, component.label, component.load_label,
        component.poles, component.amps, component.curve,
        component.breaking_capacity, component.sensitivity, component.spd_type,
        component.ct_ratio, component.meter_accuracy,
    ]
    return " ".join(str(value) for value in values if value and str(value) != "-")


def effective_component_manufacturer(project: ProjectDocument, component: Component) -> str:
    """The component selection is authoritative; the project brand is only a fallback."""
    return canonical_manufacturer(
        component.manufacturer or project.pricing.default_manufacturer
    )


class PricingService:
    def __init__(self, database: str | Path, progress=None, should_cancel=None):
        self.database = Path(database)
        self.progress = progress or (lambda message: None)
        self.should_cancel = should_cancel or (lambda: False)
        self._catalog_ready = False

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
            ensure_catalog_schema(connection)
            migrated = classify_products_in_database(connection, only_unclassified=True)
            if migrated:
                self.progress(f"Catalog taxonomy prepared for {migrated} product(s)")
            connection.commit()
            self._catalog_ready = True
            counts = {}
            for table in ("manufacturers", "ppl_editions", "products"):
                counts[table] = int(connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0])
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
            available_editions = self._available_editions(connection, project)
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
                    self.progress(
                        f"Matching {panel.reference}: {component_description(component)} "
                        f"[{manufacturer}]"
                    )
                    results.append(self._match(
                        connection, aliases, component, manufacturer, year, half,
                        product_cache, recommendation_editions=available_editions,
                        family_preferences=project.pricing.family_preferences,
                    ))
        project.match_results = results
        payload = self._pricing_payload(project, results)
        atomic_write_json(output_path, payload)
        return payload

    def _edition(
        self, connection: sqlite3.Connection, manufacturer: str, year: int, half: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT pe.id, pe.currency, pe.year, pe.half, m.name AS manufacturer
            FROM ppl_editions pe
            JOIN manufacturers m ON m.id = pe.manufacturer_id
            WHERE UPPER(m.name) = ? AND pe.year = ? AND pe.half = ?
            """,
            (manufacturer.upper(), year, half),
        ).fetchone()

    def _available_editions(
        self, connection: sqlite3.Connection, project: ProjectDocument
    ) -> list[dict]:
        selected: dict[str, dict] = {}
        for manufacturer, config in project.pricing.editions.items():
            row = self._edition(
                connection, manufacturer, int(config.get("year", datetime.now().year)),
                str(config.get("half", "H1")).upper(),
            )
            if row:
                selected[str(manufacturer).upper()] = dict(row)
        rows = connection.execute(
            """
            SELECT pe.id, pe.currency, pe.year, pe.half, m.name AS manufacturer
            FROM ppl_editions pe
            JOIN manufacturers m ON m.id=pe.manufacturer_id
            ORDER BY UPPER(m.name), pe.year DESC, pe.half DESC
            """
        ).fetchall()
        for row in rows:
            manufacturer = str(row["manufacturer"]).upper()
            selected.setdefault(manufacturer, dict(row))
        return list(selected.values())

    @staticmethod
    def _load_products(
        connection: sqlite3.Connection,
        edition_id: int,
        product_cache: dict[int, list[sqlite3.Row]],
    ) -> list[sqlite3.Row]:
        rows = product_cache.get(edition_id)
        if rows is None:
            rows = connection.execute(
                """
                SELECT p.*, pf.family_code
                FROM products p
                LEFT JOIN product_families pf ON pf.id=p.family_id
                WHERE p.ppl_edition_id=?
                """,
                (edition_id,),
            ).fetchall()
            product_cache[edition_id] = rows
        return rows

    def _match(
        self, connection, aliases, component, manufacturer, year, half,
        product_cache, recommendation_editions=None, family_preferences=None,
    ) -> MatchResult:
        ensure_catalog_schema(connection)
        if not self._catalog_ready and not product_cache:
            classify_products_in_database(connection, only_unclassified=True)
            self._catalog_ready = True
        ai_suggested = bool(component.ai_review_required)
        edition = self._edition(connection, manufacturer, year, half)
        edition_name = f"{manufacturer} {year} {half}"
        if not edition:
            return MatchResult(
                component.id, manufacturer, edition_name, quantity=component.quantity,
                tier="NF", review_status="unmatched",
                note="Price-list edition is not installed",
            )
        edition_id, currency = int(edition["id"]), str(edition["currency"])
        rows = self._load_products(connection, edition_id, product_cache)
        if component.forced_reference:
            row = next((
                item for item in rows
                if str(item["reference"]).lower() == component.forced_reference.lower()
                or str(item["order_number"] or "").lower() == component.forced_reference.lower()
            ), None)
            if not row and component.forced_unit_price is None:
                return MatchResult(
                    component.id, manufacturer, edition_name, quantity=component.quantity,
                    currency=currency, tier="NF", review_status="unmatched",
                    note=f"Forced reference not found: {component.forced_reference}",
                )
            price = max(0.0, float(
                component.forced_unit_price
                if component.forced_unit_price is not None
                else row["price"]
            ))
            selected_currency = component.forced_currency or currency
            recommendations = self._recommendations(
                connection, aliases, component, manufacturer,
                recommendation_editions or [dict(edition)], product_cache,
                exclude_reference=component.forced_reference,
                family_preferences=family_preferences,
            )
            return MatchResult(
                component.id, manufacturer, edition_name,
                chosen_reference=component.forced_reference,
                chosen_description=str(row["description"] if row else "Manual catalog item"),
                score=1.0, tier="MANUAL" if component.forced_unit_price is not None else "FORCED",
                unit_price=price, quantity=component.quantity,
                total_price=round(price * component.quantity, 2), currency=selected_currency,
                review_status="ai_suggested" if ai_suggested else "manual_selection",
                recommendations=recommendations,
                note=(
                    f"AI shortlist suggestion — verify before export. {component.ai_suggestion_note}".strip()
                    if ai_suggested else
                    "Manually selected catalog reference and unit price"
                    if component.forced_unit_price is not None
                    else "Manually selected catalog reference"
                ),
            )

        required = _component_spec(component)
        query_tokens = self._normalize_tokens(component_description(component), aliases)
        candidates: list[CandidateMatch] = []
        for row_index, row in enumerate(rows):
            if row_index % 250 == 0:
                self._check_cancelled()
            spec = row_product_spec(row, manufacturer)
            compatible, structured_bonus, _ = _exact_compatibility(required, spec)
            if not compatible:
                continue
            product_tokens = self._normalize_tokens(
                f"{row['reference']} {row['description']}", aliases
            )
            overlap = query_tokens.intersection(product_tokens)
            if required.category not in STRUCTURED_CATEGORIES and not overlap:
                continue
            recall = len(overlap) / max(1, len(query_tokens))
            precision = len(overlap) / max(1, min(len(product_tokens), len(query_tokens) * 2))
            score = min(1.0, 0.68 * recall + 0.18 * precision + structured_bonus)
            score += self._family_preference_bonus(
                family_preferences, manufacturer, required.category, spec.family
            )
            candidates.append(self._candidate(
                row, spec, dict(edition), score=round(min(1.0, score), 4),
                recommendation_kind="exact", reason="Exact structured specification",
            ))
        candidates.sort(key=lambda item: (
            -item.score,
            -self._family_preference_bonus(
                family_preferences, manufacturer, required.category, item.family
            ),
            item.price,
            item.reference,
        ))
        top = candidates[:6]
        recommendations = self._recommendations(
            connection, aliases, component, manufacturer,
            recommendation_editions or [dict(edition)], product_cache,
            exclude_reference=top[0].reference if top else "",
            family_preferences=family_preferences,
        )
        if not top or top[0].score < 0.35:
            return MatchResult(
                component.id, manufacturer, edition_name,
                alternatives=top[:5], recommendations=recommendations,
                quantity=component.quantity, currency=currency, tier="NF",
                review_status="unmatched", note="No exact compatible catalog product",
            )
        chosen = top[0]
        preferred_over_runner_up = bool(
            len(top) > 1
            and self._family_preference_bonus(
                family_preferences, manufacturer, required.category, chosen.family
            )
            > self._family_preference_bonus(
                family_preferences, manufacturer, required.category, top[1].family
            )
        )
        ambiguous = (
            len(top) > 1
            and top[1].score >= chosen.score - 0.02
            and not preferred_over_runner_up
        )
        tier = "T1" if chosen.score >= 0.75 and not ambiguous else "T2"
        status = "exact_match" if tier == "T1" else "check_candidate"
        note = (
            "Multiple exact compatible catalog families have equivalent scores"
            if ambiguous else
            "Exact structured catalog match" if tier == "T1" else
            "Exact candidate requires review"
        )
        return MatchResult(
            component.id, manufacturer, edition_name,
            chosen_reference=chosen.reference, chosen_description=chosen.description,
            alternatives=top[1:6], recommendations=recommendations, score=chosen.score,
            tier=tier, unit_price=chosen.price, quantity=component.quantity,
            total_price=round(chosen.price * component.quantity, 2), currency=currency,
            review_status=status, note=note,
        )

    def _recommendations(
        self, connection, aliases, component, requested_manufacturer,
        editions, product_cache, exclude_reference="", family_preferences=None,
    ) -> list[CandidateMatch]:
        required = _component_spec(component)
        if required.category not in STRUCTURED_CATEGORIES:
            return []
        query_tokens = self._normalize_tokens(component_description(component), aliases)
        recommendations: list[CandidateMatch] = []
        for edition in editions:
            edition_id = int(edition["id"])
            manufacturer = str(edition["manufacturer"]).upper()
            for row in self._load_products(connection, edition_id, product_cache):
                if exclude_reference and str(row["reference"]).lower() == exclude_reference.lower():
                    continue
                spec = row_product_spec(row, manufacturer)
                if spec.category != required.category or spec.product_kind != "standalone":
                    continue
                if required.poles != "-" and spec.poles != required.poles:
                    continue
                if (
                    required.category == "SPD"
                    and required.spd_type != "-"
                    and spec.spd_type != required.spd_type
                ):
                    continue
                if (
                    required.category in RESIDUAL_CATEGORIES
                    and required.sensitivity_ma is not None
                    and spec.sensitivity_ma != required.sensitivity_ma
                ):
                    continue
                if required.rated_current_a is not None and spec.rated_current_a is None:
                    continue

                same_manufacturer = manufacturer == requested_manufacturer.upper()
                current_exact = spec.rated_current_a == required.rated_current_a
                current_delta = (
                    (spec.rated_current_a or 0.0) - (required.rated_current_a or 0.0)
                    if required.rated_current_a is not None else 0.0
                )
                engineering_review = False
                reasons: list[str] = []
                if current_exact:
                    rating_kind = "exact"
                    reasons.append("requested current")
                elif required.rated_current_a is not None:
                    rating_kind = "nearest"
                    engineering_review = True
                    if current_delta > 0 and required.category in SAFE_CURRENT_UPSIZE:
                        reasons.append(
                            f"engineering review: higher current ({required.rated_current_a:g}A → "
                            f"{spec.rated_current_a:g}A)"
                        )
                    else:
                        reasons.append(
                            f"engineering rating change ({required.rated_current_a:g}A → "
                            f"{spec.rated_current_a:g}A)"
                        )
                else:
                    rating_kind = "exact"

                if (
                    required.category in CURVE_CATEGORIES
                    and required.curve != "-"
                    and spec.curve != required.curve
                ):
                    engineering_review = True
                    reasons.append(f"curve {required.curve} → {spec.curve}")
                if (
                    required.category in BREAKING_CAPACITY_CATEGORIES
                    and
                    required.breaking_capacity_ka is not None
                    and spec.breaking_capacity_ka is not None
                    and spec.breaking_capacity_ka < required.breaking_capacity_ka
                ):
                    engineering_review = True
                    reasons.append("lower breaking capacity")

                prefix = "same_manufacturer" if same_manufacturer else "other_manufacturer"
                kind = f"{prefix}_{rating_kind}"
                product_tokens = self._normalize_tokens(
                    f"{row['reference']} {row['description']}", aliases
                )
                overlap = len(query_tokens.intersection(product_tokens))
                score = 0.55 + (0.18 if same_manufacturer else 0.0) + (0.20 if current_exact else 0.0)
                score += min(0.06, overlap * 0.01)
                score += self._family_preference_bonus(
                    family_preferences, manufacturer, required.category, spec.family
                )
                if required.rated_current_a and spec.rated_current_a and not current_exact:
                    score -= min(0.22, abs(current_delta) / max(required.rated_current_a, 1) * 0.12)
                if engineering_review:
                    score -= 0.12
                reason = (
                    ("Same manufacturer; " if same_manufacturer else "Different manufacturer; ")
                    + "; ".join(reasons or ["compatible structured specification"])
                )
                recommendations.append(self._candidate(
                    row, spec, edition, score=round(max(0.0, min(1.0, score)), 4),
                    recommendation_kind=kind, reason=reason,
                    requires_engineering_review=engineering_review,
                ))

        group_order = {
            "same_manufacturer_exact": 0,
            "same_manufacturer_nearest": 1,
            "other_manufacturer_exact": 2,
            "other_manufacturer_nearest": 3,
        }
        recommendations.sort(key=lambda item: (
            item.requires_engineering_review,
            group_order.get(item.recommendation_kind, 9),
            -item.score,
            item.price,
            item.reference,
        ))
        selected: list[CandidateMatch] = []
        per_group: defaultdict[str, int] = defaultdict(int)
        seen: set[tuple[str, str]] = set()
        for candidate in recommendations:
            identity = (candidate.manufacturer, candidate.reference.lower())
            if identity in seen or per_group[candidate.recommendation_kind] >= 2:
                continue
            selected.append(candidate)
            seen.add(identity)
            per_group[candidate.recommendation_kind] += 1
            if len(selected) >= 8:
                break
        return selected

    @staticmethod
    def _family_preference_bonus(
        preferences: dict[str, list[str]] | None,
        manufacturer: str,
        category: str,
        family: str,
    ) -> float:
        """Apply a small, explicit tie-break without weakening spec checks.

        Keys may be ``MANUFACTURER:CATEGORY`` (preferred), ``CATEGORY``, or
        ``*``. Values are ordered catalog family codes. The preference only
        orders already-compatible products; it can never make an incompatible
        rating, curve, residual sensitivity, or SPD type eligible.
        """
        if not preferences or not family:
            return 0.0
        normalized = {
            str(key).strip().upper(): [str(value).strip().upper() for value in values]
            for key, values in preferences.items()
        }
        selected = (
            normalized.get(f"{manufacturer.upper()}:{category.upper()}")
            or normalized.get(category.upper())
            or normalized.get("*")
            or []
        )
        family_name = family.upper()
        if family_name not in selected:
            return 0.0
        return max(0.01, 0.10 - selected.index(family_name) * 0.03)

    @staticmethod
    def _candidate(
        row: sqlite3.Row, spec: ProductSpec, edition: dict, *, score: float,
        recommendation_kind: str, reason: str,
        requires_engineering_review: bool = False,
    ) -> CandidateMatch:
        amps = f"{spec.rated_current_a:g}A" if spec.rated_current_a is not None else "-"
        sensitivity = (
            f"{spec.sensitivity_ma:g}mA" if spec.sensitivity_ma is not None else "-"
        )
        return CandidateMatch(
            reference=str(row["reference"]), description=str(row["description"]),
            price=float(row["price"]), currency=str(edition["currency"]), score=score,
            manufacturer=str(edition["manufacturer"]).upper(),
            edition=f"{edition['manufacturer']} {edition['year']} {edition['half']}",
            year=int(edition["year"]), half=str(edition["half"]), family=spec.family,
            category=spec.category, product_kind=spec.product_kind, poles=spec.poles,
            amps=amps, sensitivity=sensitivity,
            recommendation_kind=recommendation_kind, reason=reason,
            requires_engineering_review=requires_engineering_review,
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
        totals: defaultdict[str, float] = defaultdict(float)
        for panel in project.panels:
            items = []
            panel_totals: defaultdict[str, float] = defaultdict(float)
            for component in panel.components:
                if component.review_status == "excluded":
                    continue
                result = by_component.get(component.id)
                if not result:
                    continue
                row = asdict(result)
                row["description"] = component_description(component)
                row["panel_id"] = panel.id
                items.append(row)
                if result.currency:
                    panel_totals[result.currency] += result.total_price
                    totals[result.currency] += result.total_price
            panel_totals_payload = {
                currency: round(value, 2) for currency, value in sorted(panel_totals.items())
            }
            panels.append({
                "panel_id": panel.id,
                "panel_reference": panel.reference,
                "ht": next(iter(panel_totals_payload.values())) if len(panel_totals_payload) == 1 else None,
                "totals_by_currency": panel_totals_payload,
                "items": items,
            })
        totals_by_currency = {}
        for currency, subtotal in sorted(totals.items()):
            tva = round(subtotal * project.pricing.tva_rate, 2)
            totals_by_currency[currency] = {
                "grand_ht": round(subtotal, 2),
                "tva": tva,
                "grand_ttc": round(subtotal + tva, 2),
            }
        single = next(iter(totals_by_currency.values())) if len(totals_by_currency) == 1 else None
        return {
            "schema_version": 2,
            "project_id": project.id,
            "project_reference": project.reference,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "tva_rate": project.pricing.tva_rate,
            "currencies": sorted(totals_by_currency),
            "totals_by_currency": totals_by_currency,
            "grand_ht": single["grand_ht"] if single else None,
            "tva": single["tva"] if single else None,
            "grand_ttc": single["grand_ttc"] if single else None,
            "panels": panels,
        }
