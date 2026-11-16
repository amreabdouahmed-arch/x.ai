"""Run the independent, deterministic health-claim benchmark.

This benchmark is intentionally separate from the knowledge-base entries.
It measures routing/safety behavior and never calls the live web.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from agent import (  # noqa: E402
    _has_ambiguous_negation,
    _urgent_symptom,
    _unsafe_medication_change,
)
from tools import check_rumor  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(Path(__file__).with_name("health_claims.json")))
    args = parser.parse_args()
    cases = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        claim = case["claim"]
        if _urgent_symptom(claim):
            actual = "urgent_care"
        elif _unsafe_medication_change(claim):
            actual = "unsafe_advice"
        elif _has_ambiguous_negation(claim):
            actual = "unverified"
        else:
            actual = check_rumor(claim).get("verdict", "unverified")
        expected = case["label"]
        rows.append({
            "id": case["id"],
            "expected": expected,
            "actual": actual,
            "match": actual == expected,
            "risk": case["risk"],
        })
    summary = {
        "total": len(rows),
        "matches": sum(row["match"] for row in rows),
        "accuracy": round(sum(row["match"] for row in rows) / len(rows), 3) if rows else 0,
        "high_risk_mismatches": [
            row for row in rows if not row["match"] and row["risk"] == "high"
        ],
    }
    print(json.dumps({"summary": summary, "results": rows},
                     ensure_ascii=False, indent=2))
    return 0 if not summary["high_risk_mismatches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
