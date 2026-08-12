from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dataset_benchmark import write_dataset_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Score a directory of extracted projects against offer truth")
    parser.add_argument("expected_offer")
    parser.add_argument("predictions_dir")
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest", default="")
    arguments = parser.parse_args()
    report = write_dataset_report(
        arguments.expected_offer,
        arguments.predictions_dir,
        arguments.out,
        arguments.manifest or None,
    )
    print(json.dumps(report["aggregate"], indent=2))
    print("Mapped panels:", ", ".join(report["mapped_panels"]))
    print("Missing panels:", ", ".join(report["missing_prediction_panels"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
