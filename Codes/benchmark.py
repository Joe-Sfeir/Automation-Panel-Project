from __future__ import annotations

import argparse
import json
from dataclasses import asdict
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.benchmark import run_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare an extracted project with a verified BOM project")
    parser.add_argument("predicted")
    parser.add_argument("verified")
    parser.add_argument("--out", default="benchmark-report.json")
    arguments = parser.parse_args()
    report = run_benchmark(arguments.predicted, arguments.verified, arguments.out)
    print(json.dumps(asdict(report), indent=2))
    return 0 if report.f1 >= 0.95 and report.silent_high_confidence_contradictions == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
