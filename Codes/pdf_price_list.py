"""Deterministic readers for supported, text-based PDF price lists."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from pathlib import Path

import pymupdf


_PRICE_RE = re.compile(r"(?<![\d.])([0-9][0-9,]*(?:\.[0-9]{1,2})?)(?![\d.])")
_ORDER_RE = re.compile(r"\b\d{6,10}\b")


@dataclass
class _PhoenixMarker:
    page: int
    y0: float
    y1: float
    reference: str
    order_number: str
    tail: str
    price: float | None


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _reference(value: str) -> str:
    clean = _one_line(value).rstrip(" ,;:")
    # Long item codes are visually wrapped in the PDF. A line break after a
    # separator is formatting, not part of the commercial reference.
    return re.sub(r"([\-/])\s+", r"\1", clean).upper()


def _price(value: str) -> float | None:
    matches = list(_PRICE_RE.finditer(str(value or "")))
    if not matches:
        return None
    return round(float(matches[-1].group(1).replace(",", "")), 2)


def _strip_last_price(value: str) -> str:
    matches = list(_PRICE_RE.finditer(str(value or "")))
    if not matches:
        return _one_line(value)
    match = matches[-1]
    return _one_line(value[:match.start()] + " " + value[match.end():])


def _deduplicate(products: list[dict]) -> list[dict]:
    by_reference: dict[str, dict] = {}
    for product in products:
        key = str(product["reference"]).strip().upper()
        previous = by_reference.get(key)
        if previous is None:
            by_reference[key] = product
            continue
        if str(previous.get("order_number")) != str(product.get("order_number")):
            raise ValueError(
                f"Conflicting PDF rows reuse {product['reference']} with different "
                "order numbers"
            )
        if float(previous["price"]) != float(product["price"]):
            selected = max(
                (previous, product), key=lambda row: float(row["price"])
            )
            print(
                f"  WARNING: {product['reference']} / {product.get('order_number')} "
                f"is listed at {previous['price']:.2f} and {product['price']:.2f}; "
                f"using the higher price {selected['price']:.2f}"
            )
            by_reference[key] = selected
            continue
        if len(str(product["description"])) > len(str(previous["description"])):
            by_reference[key] = product
    return list(by_reference.values())


def _validate(products: list[dict], *, minimum: int, source: str) -> list[dict]:
    products = _deduplicate(products)
    if len(products) < minimum:
        raise ValueError(
            f"{source} extraction produced only {len(products)} products; "
            "the PDF layout may have changed"
        )
    for product in products:
        if not str(product.get("reference", "")).strip():
            raise ValueError(f"{source} extraction produced a blank reference")
        if not str(product.get("description", "")).strip():
            raise ValueError(f"{source} extraction produced a blank description")
        if float(product.get("price", 0) or 0) <= 0:
            raise ValueError(
                f"{source} extraction produced an invalid price for "
                f"{product.get('reference', '<unknown>')}"
            )
    return products


def _read_mbs(document: pymupdf.Document) -> list[dict]:
    products: list[dict] = []
    row_re = re.compile(
        r"^\s*(MBS-.+?)\s+([0-9][0-9-]*),\s*(.+?)\s+([0-9]+\.[0-9]{2})\s*$"
    )
    for page_number, page in enumerate(document, start=1):
        page_rows = 0
        for line in page.get_text("text", sort=True).splitlines():
            match = row_re.match(_one_line(line))
            if not match:
                continue
            reference, order_number, specifications, raw_price = match.groups()
            products.append({
                "reference": _one_line(reference),
                "order_number": order_number,
                "description": _one_line(
                    f"Low-voltage current transformer; {specifications}"
                ),
                "price": round(float(raw_price), 2),
            })
            page_rows += 1
        print(f"    Page {page_number}: {page_rows} priced CT row(s)")
    return _validate(products, minimum=20, source="MBS PDF")


def _phoenix_title(blocks: list[tuple], marker_y: float) -> str:
    ignored = (
        "ALL PRICES", "PICTURE", "ITEM CODE", "ORDER NO", "DESCRIPTION",
        "PRICE", "AL-BONIAN", "TAYOUNEH", "2026",
    )
    candidates: list[tuple[float, str]] = []
    for block in blocks:
        x0, y0, _x1, y1, text = block[:5]
        clean = _one_line(text)
        upper = clean.upper()
        if y1 >= marker_y or x0 > 120 or not clean or "PHC-" in upper:
            continue
        if any(token in upper for token in ignored):
            continue
        candidates.append((y1, clean))
    return candidates[-1][1] if candidates else "Phoenix Contact catalog item"


def _phoenix_markers(blocks: list[tuple], page_number: int) -> list[_PhoenixMarker]:
    markers: list[_PhoenixMarker] = []
    consumed: set[int] = set()
    for index, block in enumerate(blocks):
        if index in consumed:
            continue
        x0, y0, _x1, y1, text = block[:5]
        clean = _one_line(text)
        if x0 >= 270:
            continue
        start = clean.upper().find("PHC-")
        if start < 0:
            continue
        candidate = clean[start:]
        if not _ORDER_RE.search(candidate):
            # Wide description blocks can sit between the two visual lines of
            # a wrapped item code. Search a few nearby blocks in the item/order
            # columns instead of assuming physical adjacency in PDF block order.
            for next_index in range(index + 1, min(index + 4, len(blocks))):
                next_block = blocks[next_index]
                nx0, ny0, _nx1, ny1, next_text = next_block[:5]
                next_clean = _one_line(next_text)
                if ny0 > y1 + 22:
                    break
                if nx0 >= 270 or not _ORDER_RE.search(next_clean):
                    continue
                candidate = _one_line(candidate + " " + next_clean)
                y1 = max(y1, ny1)
                consumed.add(next_index)
                break

        # A PDF block can contain a heading before the first item. Split at each
        # PHC reference so every marker is considered independently.
        segments = re.split(r"(?=PHC-)", candidate, flags=re.I)
        for segment in segments:
            segment = _one_line(segment)
            if not segment.upper().startswith("PHC-"):
                continue
            order = _ORDER_RE.search(segment)
            if order is None:
                continue
            reference = _reference(segment[:order.start()])
            order_number = order.group(0)
            tail = _one_line(segment[order.end():])
            if not reference or len(reference) > 100:
                continue
            markers.append(_PhoenixMarker(
                page=page_number,
                y0=float(y0),
                y1=float(y1),
                reference=reference,
                order_number=order_number,
                tail=tail,
                price=_price(tail),
            ))
    return sorted(markers, key=lambda item: (item.y0, item.reference))


def _phoenix_word_markers(
    page: pymupdf.Page,
    page_number: int,
    existing: list[_PhoenixMarker],
) -> list[_PhoenixMarker]:
    """Add visual item rows that share a merged order/price cell.

    Some Phoenix marking tables print several distinct references beside one
    vertically merged order-number and price cell. PDF blocks cannot represent
    that relationship, so use the original word coordinates and nearest column
    cells to recover each visible reference row.
    """
    words = page.get_text("words", sort=True)
    line_groups: dict[tuple[int, int], list[tuple]] = {}
    for word in words:
        line_groups.setdefault((int(word[5]), int(word[6])), []).append(word)
    lines = []
    for line_words in line_groups.values():
        line_words.sort(key=lambda word: (word[0], word[7]))
        lines.append({
            "words": line_words,
            "x0": min(float(word[0]) for word in line_words),
            "y0": min(float(word[1]) for word in line_words),
            "y1": max(float(word[3]) for word in line_words),
            "text": _one_line(" ".join(str(word[4]) for word in line_words)),
        })
    lines.sort(key=lambda line: (line["y0"], line["x0"]))

    order_headers = [float(word[0]) for word in words if str(word[4]).upper() == "ORDER"]
    price_headers = [float(word[0]) for word in words if str(word[4]).upper() == "PRICE"]
    if not order_headers or not price_headers:
        return existing
    order_x = statistics.median(order_headers)
    price_x = statistics.median(price_headers)

    order_cells = [
        (float(word[1] + word[3]) / 2, str(word[4]))
        for word in words
        if _ORDER_RE.fullmatch(str(word[4]))
        and abs(float(word[0]) - order_x) <= 75
        and 55 < float(word[1]) < float(page.rect.height) - 45
    ]
    price_cells = []
    for word in words:
        raw = str(word[4]).replace(",", "")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", raw):
            continue
        if len(raw.split(".", 1)[0]) > 5:
            continue
        # Pcs./Pkt. is immediately left of Price on dense marking tables.
        # Accept only the price column itself, not the nearby packet quantity.
        if not (price_x - 8 <= float(word[0]) <= price_x + 75):
            continue
        if not (55 < float(word[1]) < float(page.rect.height) - 45):
            continue
        price_cells.append((float(word[1] + word[3]) / 2, round(float(raw), 2)))

    by_reference = {marker.reference.upper(): marker for marker in existing}
    for line_index, line in enumerate(lines):
        line_words = line["words"]
        phc_index = next((
            index for index, word in enumerate(line_words)
            if (
                str(word[4]).upper().startswith("PHC-")
                or str(word[4]).upper() == "THERMOMARK"
            )
            and float(word[0]) < 270
            and float(word[1]) > 65
        ), None)
        if phc_index is None:
            continue
        reference_words = []
        for word in line_words[phc_index:]:
            token = str(word[4])
            if _ORDER_RE.fullmatch(token) or float(word[0]) >= order_x - 8:
                break
            reference_words.append(token)
        reference = _reference(" ".join(reference_words))
        if not reference:
            continue

        if reference.endswith(("-", "/")):
            for next_line in lines[line_index + 1:line_index + 4]:
                if next_line["y0"] > line["y1"] + 22:
                    break
                if "PHC-" in next_line["text"].upper():
                    continue
                continuation = []
                for word in next_line["words"]:
                    token = str(word[4])
                    if _ORDER_RE.fullmatch(token) or float(word[0]) >= order_x - 8:
                        break
                    if float(word[0]) >= line["x0"] - 20:
                        continuation.append(token)
                if continuation:
                    reference = _reference(reference + " " + " ".join(continuation))
                    break

        center_y = float(line["y0"] + line["y1"]) / 2
        group_key = re.sub(
            r"\s+(?:CC:\d+|YE:[A-Z]|LGS\d+(?:-\d+)?)$", "", reference,
            flags=re.I,
        )
        group_centers = []
        if group_key != reference:
            for other in lines:
                other_words = other["words"]
                other_phc = next((
                    idx for idx, word in enumerate(other_words)
                    if (
                        str(word[4]).upper().startswith("PHC-")
                        or str(word[4]).upper() == "THERMOMARK"
                    )
                    and float(word[0]) < 270
                    and float(word[1]) > 65
                ), None)
                if other_phc is None:
                    continue
                raw_parts = []
                for word in other_words[other_phc:]:
                    token = str(word[4])
                    if _ORDER_RE.fullmatch(token) or float(word[0]) >= order_x - 8:
                        break
                    raw_parts.append(token)
                other_reference = _reference(" ".join(raw_parts))
                other_key = re.sub(
                    r"\s+(?:CC:\d+|YE:[A-Z]|LGS\d+(?:-\d+)?)$", "",
                    other_reference, flags=re.I,
                )
                if other_key == group_key:
                    group_centers.append(float(other["y0"] + other["y1"]) / 2)
        association_y = statistics.median(group_centers) if group_centers else center_y
        nearest_order = min(order_cells, key=lambda cell: abs(cell[0] - association_y), default=None)
        nearest_price = min(price_cells, key=lambda cell: abs(cell[0] - association_y), default=None)
        if nearest_order is None or nearest_price is None:
            continue
        if abs(nearest_order[0] - association_y) > 75 or abs(nearest_price[0] - association_y) > 75:
            continue

        same_visual_row = min(
            existing, key=lambda marker: abs(marker.y0 - float(line["y0"])),
            default=None,
        )
        if (
            same_visual_row is not None
            and abs(same_visual_row.y0 - float(line["y0"])) <= 2.5
        ):
            if (
                reference.startswith(same_visual_row.reference)
                and len(reference) > len(same_visual_row.reference)
            ):
                by_reference.pop(same_visual_row.reference.upper(), None)
                same_visual_row.reference = reference
                by_reference[reference.upper()] = same_visual_row
            same_visual_row.price = nearest_price[1]
            continue

        current = by_reference.get(reference.upper())
        if current is not None:
            if current.price is None:
                current.price = nearest_price[1]
            continue
        marker = _PhoenixMarker(
            page=page_number,
            y0=float(line["y0"]),
            y1=float(line["y1"]),
            reference=reference,
            order_number=nearest_order[1],
            tail="",
            price=nearest_price[1],
        )
        existing.append(marker)
        by_reference[reference.upper()] = marker

    return sorted(existing, key=lambda item: (item.y0, item.reference))


def _read_phoenix(document: pymupdf.Document) -> list[dict]:
    products: list[dict] = []
    for page_number, page in enumerate(document, start=1):
        blocks = page.get_text("blocks", sort=True)
        markers = _phoenix_word_markers(
            page, page_number, _phoenix_markers(blocks, page_number)
        )
        page_rows = 0
        for marker_index, marker in enumerate(markers):
            next_y = (
                markers[marker_index + 1].y0 - 2
                if marker_index + 1 < len(markers)
                else min(float(page.rect.height) - 45, marker.y0 + 180)
            )
            lower_y = max(45.0, marker.y0 - 18)
            price = marker.price
            description_parts = [
                _phoenix_title(blocks, marker.y0),
                _strip_last_price(marker.tail),
            ]
            for block in blocks:
                x0, y0, _x1, y1, text = block[:5]
                clean = _one_line(text)
                upper = clean.upper()
                if y1 < lower_y or y0 >= next_y or not clean:
                    continue
                if "PHC-" in upper or "ITEM CODE" in upper or "ORDER NO" in upper:
                    continue
                if x0 > page.rect.width * 0.72 and re.fullmatch(
                    r"[0-9][0-9,]*(?:\.[0-9]{1,2})?", clean
                ):
                    if price is None:
                        price = _price(clean)
                    continue
                if x0 >= page.rect.width * 0.40 and not upper.startswith("PRICE"):
                    description_parts.append(clean)

            if price is None:
                continue
            unique_parts: list[str] = []
            for part in description_parts:
                part = _one_line(part)
                if not part or part in unique_parts:
                    continue
                unique_parts.append(part)
            products.append({
                "reference": marker.reference,
                "order_number": marker.order_number,
                "description": "; ".join(unique_parts)[:1200],
                "price": price,
            })
            page_rows += 1
        print(f"    Page {page_number}: {page_rows} priced row(s)")
    return _validate(products, minimum=100, source="Phoenix Contact PDF")


def read_pdf_price_list(filepath: Path, manufacturer: str) -> list[dict]:
    """Read one of the supported born-digital PDF catalogue layouts."""
    print(f"  Loading PDF: {filepath.name} ...")
    document = pymupdf.open(filepath)
    try:
        if document.needs_pass:
            raise ValueError(f"Encrypted PDF is not supported: {filepath.name}")
        pages_with_text = sum(
            bool(page.get_text("text").strip()) for page in document
        )
        print(
            f"  PDF pages: {document.page_count}  |  "
            f"text-layer pages: {pages_with_text}"
        )
        if pages_with_text != document.page_count:
            raise ValueError(
                "This PDF contains scanned or textless pages. OCR review is required "
                "before it can be imported safely."
            )
        brand = str(manufacturer or "").upper().strip()
        if brand == "MBS":
            products = _read_mbs(document)
        elif brand in {"PHOENIX", "PHOENIX CONTACT"}:
            products = _read_phoenix(document)
        else:
            raise ValueError(
                "PDF import currently supports MBS and PHOENIX CONTACT layouts only"
            )
    finally:
        document.close()
    print(f"\n  Total extracted across all PDF pages: {len(products)}")
    return products
