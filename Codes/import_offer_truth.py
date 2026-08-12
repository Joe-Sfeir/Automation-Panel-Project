from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.offer_truth import import_offer_truth
from app.persistence import atomic_write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a combined client offer as benchmark truth")
    parser.add_argument("workbook")
    parser.add_argument("--out", required=True)
    arguments = parser.parse_args()
    payload = import_offer_truth(arguments.workbook)
    atomic_write_json(arguments.out, payload)
    print(f"Imported {len(payload['panels'])} panel sections -> {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
