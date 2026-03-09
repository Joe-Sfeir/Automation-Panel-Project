#!/usr/bin/env python3
"""
export_json.py  —  Export offer.json → styled Excel client offer.

Reads the structured offer JSON produced by match_offer_json.py / run_match()
and writes a professionally formatted Excel matching the exact style of the
TECHNELEC / ELIE client offer (KORNET EL HAMRA template):
  • Column widths, row heights, fonts, alignments all match original
  • Multi-panel: each DB gets its own clearly labelled section
  • pricing_mode="total":    shows only panel HT + TTC (no per-item prices)
  • pricing_mode="itemized": adds Qty | Unit Price | Line Total per item

Usage (CLI):
    python export_json.py --input offer.json --out offer_client.xlsx
    python export_json.py --input offer.json --out offer_client.xlsx \\
                          --att "M. Khalil" --date 09/03/2026

Usage (PySide6 / Python API):
    from export_json import export_offer_json
    path = export_offer_json("offer.json", "offer_client.xlsx",
                             att="M. Khalil", date_str="09/03/2026")
"""

import argparse
import json
import sys
from datetime import date as date_cls
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ─────────────────────────────────────────────────────────────────────────────
#  LAYOUT CONSTANTS  (exact values from original offer file)
# ─────────────────────────────────────────────────────────────────────────────

# Column letter → width in Excel character units
_COL_WIDTHS_TOTAL = {
    "A": 14.66,   # hidden / blank
    "B":  1.88,   # thin spacer
    "C": 43.99,   # description / panel name / text blocks
    "D": 22.99,   # brand / "Date:" labels
    "E": 10.10,   # qty / "TTC:" label
    "F": 24.43,   # price amounts
}

# Extra columns appended in "itemized" mode
_COL_WIDTHS_ITEMIZED = {
    **_COL_WIDTHS_TOTAL,
    "G": 14.00,   # unit price
    "H": 16.00,   # line total
}

ROW_H_STD    = 15.6   # standard data row
ROW_H_THIN   =  8.1   # thin separator between panels
ROW_H_PANEL  = 18.0   # panel name row
ROW_H_ITEM   = 15.6   # item row
ROW_H_TITLE  = 20.0   # grand total / section titles

# Number format for Euro amounts (€ sign, 2 decimal places)
NF_EURO = u'#,##0.00 [$\u20ac-1]'

# ─────────────────────────────────────────────────────────────────────────────
#  STYLE OBJECTS
# ─────────────────────────────────────────────────────────────────────────────

def _f(bold=False, size=10, italic=False, color="000000", underline=None):
    return Font(name="Arial", bold=bold, size=size, italic=italic,
                color=color, underline=underline)

FNT_META      = _f(size=10)
FNT_META_BOLD = _f(size=10, bold=True, underline="single")
FNT_INTRO     = _f(size=10)
FNT_PANEL     = _f(size=11, bold=True)
FNT_ITEM      = _f(size=10)
FNT_BRAND     = _f(size=10)
FNT_QTY       = _f(size=10)
FNT_PRICE_LBL = _f(size=10, bold=True)
FNT_PRICE_VAL = _f(size=10, bold=True)
FNT_GRAND     = _f(size=11, bold=True)
FNT_COND_H    = _f(size=10, bold=True)
FNT_COND_B    = _f(size=9)
FNT_SIGN      = _f(size=10, bold=True)
FNT_COL_HDR   = _f(size=9,  bold=True, color="555555")   # "itemized" column headers

_AL_L  = Alignment(horizontal="left",   vertical="center")
_AL_R  = Alignment(horizontal="right",  vertical="center")
_AL_C  = Alignment(horizontal="center", vertical="center")
_AL_LW = Alignment(horizontal="left",   vertical="top", wrap_text=True)

_THIN  = Side(style="thin", color="CCCCCC")
_BDR   = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_BDR_B = Border(bottom=Side(style="thin", color="999999"))  # underline separator

FILL_PANEL_HDR = PatternFill("solid", fgColor="E8EEF4")   # light blue-grey panel header
FILL_GRAND     = PatternFill("solid", fgColor="D4E1F0")   # slightly stronger for grand total
FILL_COL_HDR   = PatternFill("solid", fgColor="F2F2F2")   # column header row (itemized)


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _cell(ws, row, col, value=None, font=None, alignment=None,
          number_format=None, fill=None, border=None):
    c = ws.cell(row=row, column=col, value=value)
    if font:         c.font         = font
    if alignment:    c.alignment    = alignment
    if number_format: c.number_format = number_format
    if fill:         c.fill         = fill
    if border:       c.border       = border
    return c


def _row_h(ws, row, h):
    ws.row_dimensions[row].height = h


def _blank(ws, row, h=ROW_H_THIN):
    _row_h(ws, row, h)
    return row + 1


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION WRITERS
# ─────────────────────────────────────────────────────────────────────────────

def _write_columns(ws, itemized: bool):
    widths = _COL_WIDTHS_ITEMIZED if itemized else _COL_WIDTHS_TOTAL
    for letter, w in widths.items():
        ws.column_dimensions[letter].width = w


def _write_header(ws, ref: str, att: str, date_str: str) -> int:
    """Write the top letterhead/intro block. Returns next free row number."""
    r = 1
    # Row 1 — "MM."
    _cell(ws, r, 3, "MM.", font=FNT_META, alignment=_AL_L)
    _row_h(ws, r, ROW_H_STD); r += 1
    # Rows 2–4 — blank
    for _ in range(3):
        _row_h(ws, r, ROW_H_STD); r += 1
    # Row 5 — "Att :" | "Date : dd/mm/yyyy"
    _cell(ws, r, 3, "Att :", font=FNT_META, alignment=_AL_L)
    _cell(ws, r, 4, f"Date :   {date_str}", font=FNT_META, alignment=_AL_L)
    _row_h(ws, r, ROW_H_STD); r += 1
    # Row 6 — "Ref : PROJECT"  (bold + underline to match original)
    _cell(ws, r, 4, f"Ref :   {ref}", font=FNT_META_BOLD, alignment=_AL_L)
    _row_h(ws, r, ROW_H_STD); r += 1
    # Row 7 — blank
    _row_h(ws, r, ROW_H_STD); r += 1
    # Row 8 — "Dear Sir."
    _cell(ws, r, 3, "Dear Sir.", font=FNT_INTRO, alignment=_AL_L)
    _row_h(ws, r, ROW_H_STD); r += 1
    # Row 9 — blank
    _row_h(ws, r, ROW_H_THIN); r += 1
    # Rows 10–11 — intro paragraph
    _cell(ws, r, 3,
          "  As per to your request, please find hereunder our best offer for the supply of the",
          font=FNT_INTRO, alignment=_AL_L)
    _row_h(ws, r, ROW_H_STD); r += 1
    _cell(ws, r, 3,
          "electrical panelboards listed below to serve your project.",
          font=FNT_INTRO, alignment=_AL_L)
    _row_h(ws, r, ROW_H_STD); r += 1
    # Row 12 — blank
    _row_h(ws, r, ROW_H_STD); r += 1
    return r


def _write_panel(ws, panel: dict, pricing_mode: str, row: int) -> int:
    """
    Write one DB panel section.  Layout:

    [thin sep]
    [C=panel_name bold]                    [F=HT_amount €]
                             [E=TTC:]      [F=TTC_amount €]
    [thin sep]
    [C=desc] [D=brand] [E=qty]             (total mode)
    [C=desc] [D=brand] [E=qty] [G=uPrice] [H=total]  (itemized mode)
    [thin sep]
    """
    itemized = (pricing_mode == "itemized")
    pname    = panel["panel_name"]
    ht       = panel["ht"]
    ttc      = panel["ttc"]
    items    = panel.get("items", [])

    # ── thin separator before panel ───────────────────────────────────────────
    row = _blank(ws, row, ROW_H_THIN)

    # ── panel name + HT row ───────────────────────────────────────────────────
    _cell(ws, row, 3, pname, font=FNT_PANEL, alignment=_AL_L, fill=FILL_PANEL_HDR)
    _cell(ws, row, 4, None,  fill=FILL_PANEL_HDR)
    _cell(ws, row, 5, None,  fill=FILL_PANEL_HDR)
    _cell(ws, row, 6, ht,
          font=FNT_PRICE_VAL, alignment=_AL_R,
          number_format=NF_EURO, fill=FILL_PANEL_HDR)
    if itemized:
        _cell(ws, row, 7, None, fill=FILL_PANEL_HDR)
        _cell(ws, row, 8, None, fill=FILL_PANEL_HDR)
    _row_h(ws, row, ROW_H_PANEL); row += 1

    # ── TTC row ───────────────────────────────────────────────────────────────
    _cell(ws, row, 5, "TTC :", font=FNT_PRICE_LBL, alignment=_AL_R, fill=FILL_PANEL_HDR)
    _cell(ws, row, 6, ttc,
          font=FNT_PRICE_VAL, alignment=_AL_R,
          number_format=NF_EURO, fill=FILL_PANEL_HDR)
    if itemized:
        _cell(ws, row, 3, None, fill=FILL_PANEL_HDR)
        _cell(ws, row, 4, None, fill=FILL_PANEL_HDR)
        _cell(ws, row, 7, None, fill=FILL_PANEL_HDR)
        _cell(ws, row, 8, None, fill=FILL_PANEL_HDR)
    _row_h(ws, row, ROW_H_STD); row += 1

    # ── column sub-headers (itemized only) ────────────────────────────────────
    if itemized:
        _cell(ws, row, 3, "Description",  font=FNT_COL_HDR, alignment=_AL_L, fill=FILL_COL_HDR)
        _cell(ws, row, 4, "Brand",        font=FNT_COL_HDR, alignment=_AL_C, fill=FILL_COL_HDR)
        _cell(ws, row, 5, "Qty",          font=FNT_COL_HDR, alignment=_AL_C, fill=FILL_COL_HDR)
        _cell(ws, row, 7, "Unit Price",   font=FNT_COL_HDR, alignment=_AL_R, fill=FILL_COL_HDR)
        _cell(ws, row, 8, "Total",        font=FNT_COL_HDR, alignment=_AL_R, fill=FILL_COL_HDR)
        _cell(ws, row, 6, None, fill=FILL_COL_HDR)
        _row_h(ws, row, ROW_H_STD); row += 1
    else:
        # thin gap before items
        row = _blank(ws, row, ROW_H_THIN)

    # ── item rows ─────────────────────────────────────────────────────────────
    for item in items:
        _cell(ws, row, 3, item.get("desc",  ""), font=FNT_ITEM,  alignment=_AL_L)
        _cell(ws, row, 4, item.get("brand", ""), font=FNT_BRAND, alignment=_AL_L)
        _cell(ws, row, 5, item.get("qty",   0),  font=FNT_QTY,   alignment=_AL_C)

        if itemized:
            up    = item.get("unit_price", 0.0) or 0.0
            total = item.get("total",      0.0) or 0.0
            _cell(ws, row, 7, up,    font=FNT_ITEM, alignment=_AL_R, number_format=NF_EURO)
            _cell(ws, row, 8, total, font=FNT_ITEM, alignment=_AL_R, number_format=NF_EURO)

        _row_h(ws, row, ROW_H_ITEM); row += 1

    # ── thin gap after items ──────────────────────────────────────────────────
    row = _blank(ws, row, ROW_H_THIN)
    return row


def _write_grand_total(ws, grand_ht: float, grand_ttc: float,
                       tva_rate: float, row: int, itemized: bool) -> int:
    """Write the grand total summary block."""
    row = _blank(ws, row, ROW_H_THIN)

    # HT total
    _cell(ws, row, 3, "Grand Total HT :",    font=FNT_GRAND, alignment=_AL_R, fill=FILL_GRAND)
    _cell(ws, row, 4, None, fill=FILL_GRAND)
    _cell(ws, row, 5, None, fill=FILL_GRAND)
    _cell(ws, row, 6, grand_ht,
          font=FNT_GRAND, alignment=_AL_R,
          number_format=NF_EURO, fill=FILL_GRAND)
    if itemized:
        _cell(ws, row, 7, None, fill=FILL_GRAND)
        _cell(ws, row, 8, None, fill=FILL_GRAND)
    _row_h(ws, row, ROW_H_TITLE); row += 1

    # TVA line
    tva_pct = round(tva_rate * 100)
    _cell(ws, row, 3, f"TVA {tva_pct}% :",       font=FNT_GRAND, alignment=_AL_R, fill=FILL_GRAND)
    _cell(ws, row, 4, None, fill=FILL_GRAND)
    _cell(ws, row, 5, None, fill=FILL_GRAND)
    _cell(ws, row, 6, round(grand_ht * tva_rate, 2),
          font=FNT_GRAND, alignment=_AL_R,
          number_format=NF_EURO, fill=FILL_GRAND)
    if itemized:
        _cell(ws, row, 7, None, fill=FILL_GRAND)
        _cell(ws, row, 8, None, fill=FILL_GRAND)
    _row_h(ws, row, ROW_H_STD); row += 1

    # TTC total
    _cell(ws, row, 3, "Grand Total TTC :",   font=FNT_GRAND, alignment=_AL_R, fill=FILL_GRAND)
    _cell(ws, row, 4, None, fill=FILL_GRAND)
    _cell(ws, row, 5, None, fill=FILL_GRAND)
    _cell(ws, row, 6, grand_ttc,
          font=FNT_GRAND, alignment=_AL_R,
          number_format=NF_EURO, fill=FILL_GRAND)
    if itemized:
        _cell(ws, row, 7, None, fill=FILL_GRAND)
        _cell(ws, row, 8, None, fill=FILL_GRAND)
    _row_h(ws, row, ROW_H_TITLE); row += 1

    return row


_COMMERCIAL_CONDITIONS = [
    ("", ""),
    ("Commercial Conditions:", ""),
    ("", ""),
    ("B.1 Conditions of Sale", ""),
    ("",
     "This quotation is submitted subject to TECHNICAT Group Conditions of Business. "
     "All prices have been offered in conjunction with these terms and conditions, "
     "and any deviation from these terms may result in the amendment of these prices."),
    ("", ""),
    ("B.2 Validity", ""),
    ("",
     "The prices indicated are open for acceptance up till One Week from date of this proposal. "
     "Thereafter this date the tender will be subject to extensions or revisions."),
    ("", ""),
    ("B.3 Estimated Delivery", ""),
    ("", "Based on current commitments:"),
    ("", "* To be fixed later upon your priority schedule."),
    ("", "- Dispatch time is our best estimate at time of quotation, and will be confirmed upon "
         "receipt of order. This confirmed date would form our contractual offer of dispatch."),
    ("", "* Prices include transportation to the site."),
    ("", ""),
    ("B.4 Warranty", ""),
    ("", "A total warranty against any fault in the assembly is provided."),
    ("", ""),
    ("B.5 Payment", ""),
    ("", "* CASH PAYMENT."),
]


def _write_conditions(ws, row: int) -> int:
    """Write the commercial conditions block."""
    for _ in range(3):
        row = _blank(ws, row, ROW_H_STD)

    for header, body in _COMMERCIAL_CONDITIONS:
        if header:
            _cell(ws, row, 3, header, font=FNT_COND_H, alignment=_AL_L)
            _row_h(ws, row, ROW_H_STD)
        elif body:
            c = _cell(ws, row, 3, body, font=FNT_COND_B, alignment=_AL_LW)
            _row_h(ws, row, ROW_H_STD)
        else:
            _row_h(ws, row, ROW_H_THIN)
        row += 1

    return row


def _write_closing(ws, row: int) -> int:
    """Write the closing statement and signature block."""
    for _ in range(2):
        row = _blank(ws, row, ROW_H_STD)

    _cell(ws, row, 3,
          "We trust the above shall meet your acceptance and look forward to hear from you soon.",
          font=FNT_INTRO, alignment=_AL_L)
    _row_h(ws, row, ROW_H_STD); row += 1

    row = _blank(ws, row, ROW_H_STD)
    _cell(ws, row, 3, "Sincerely Yours", font=FNT_INTRO, alignment=_AL_L)
    _row_h(ws, row, ROW_H_STD); row += 1

    for _ in range(3):
        row = _blank(ws, row, ROW_H_STD)

    _cell(ws, row, 3, "ELIE BOU SERHAL", font=FNT_SIGN,    alignment=_AL_C)
    _row_h(ws, row, ROW_H_STD); row += 1
    _cell(ws, row, 3, "Electrical Eng.", font=_f(italic=True), alignment=_AL_C)
    _row_h(ws, row, ROW_H_STD); row += 1

    return row


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def export_offer_json(
    json_path: str | Path,
    out_path:  str | Path,
    att:       str = "",
    date_str:  str = "",
) -> str:
    """
    Convert offer.json → styled Excel client offer.

    Parameters
    ----------
    json_path : path to the offer JSON produced by run_match() / match_offer_json.py
    out_path  : where to write the Excel file
    att       : optional "Att :" name to show in the header (leave blank = empty)
    date_str  : optional date string, e.g. "09/03/2026" (default = today)

    Returns
    -------
    str — absolute path of the written Excel file
    """
    json_path = Path(json_path)
    out_path  = Path(out_path)

    if not json_path.exists():
        raise FileNotFoundError(f"Offer JSON not found: {json_path}")

    data = json.loads(json_path.read_text(encoding="utf-8"))

    ref          = data.get("ref", "")
    pricing_mode = data.get("pricing_mode", "total")
    grand_ht     = float(data.get("grand_ht",  0.0))
    grand_ttc    = float(data.get("grand_ttc", 0.0))
    tva_rate     = float(data.get("tva_rate",  0.11))
    panels       = data.get("panels", [])
    itemized     = pricing_mode == "itemized"

    if not date_str:
        date_str = date_cls.today().strftime("%d/%m/%Y")

    wb = Workbook()
    ws = wb.active
    ws.title = ref[:31] if ref else "Offer"

    _write_columns(ws, itemized)

    # ── letterhead ────────────────────────────────────────────────────────────
    row = _write_header(ws, ref=ref, att=att, date_str=date_str)

    # ── one section per panel / DB ────────────────────────────────────────────
    for panel in panels:
        row = _write_panel(ws, panel, pricing_mode, row)

    # ── grand total block ─────────────────────────────────────────────────────
    row = _blank(ws, row, ROW_H_STD)
    row = _write_grand_total(ws, grand_ht, grand_ttc, tva_rate, row, itemized)

    # ── commercial conditions + closing ──────────────────────────────────────
    row = _write_conditions(ws, row)
    row = _write_closing(ws, row)

    # ── print / view settings ─────────────────────────────────────────────────
    ws.sheet_view.showGridLines = False
    ws.print_area = f"A1:{get_column_letter(8 if itemized else 6)}{row}"
    ws.page_setup.fitToWidth  = 1
    ws.page_setup.orientation = "portrait"

    wb.save(str(out_path))
    print(f"  Exported offer → {out_path}  "
          f"({len(panels)} panel(s), mode={pricing_mode})")
    return str(out_path.resolve())


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Export offer.json → styled Excel client offer.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument("--input",  required=True,
                    help="offer.json produced by match_offer_json.py")
    ap.add_argument("--out",    default="offer_client.xlsx",
                    help="Output Excel file path (default: offer_client.xlsx)")
    ap.add_argument("--att",    default="",
                    help="Name for the 'Att :' field in the header")
    ap.add_argument("--date",   default="",
                    help="Date string shown in header, e.g. 09/03/2026 (default: today)")
    args = ap.parse_args()

    try:
        result = export_offer_json(
            json_path=args.input,
            out_path=args.out,
            att=args.att,
            date_str=args.date,
        )
        print(f"Done → {result}")
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"ERROR: {e}"); sys.exit(1)