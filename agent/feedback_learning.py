"""Safe feedback review and training-data export.

User feedback is evidence about the product, not medical ground truth. Only
explicitly reviewed rows are eligible for a future training set.
"""

from __future__ import annotations

import json
from pathlib import Path

ALLOWED_VERDICTS = {"rumor", "supported", "unverified"}
ALLOWED_LABELS = {"rumor", "supported", "unverified"}


def read_feedback(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        verdict = str(row.get("verdict", ""))
        claim = str(row.get("claim", "")).strip()
        if verdict not in ALLOWED_VERDICTS or not claim:
            continue
        rows.append(row)
    return rows


def export_reviewed_training_data(
    feedback_path: Path, output_path: Path
) -> int:
    """Export only human-reviewed, accepted rows as JSONL.

    A row must have ``reviewed=true``, ``accepted=true`` and an explicit
    ``label``. This prevents raw user feedback from silently changing model
    behavior.
    """
    exported = []
    for row in read_feedback(feedback_path):
        label = str(row.get("label", ""))
        if (row.get("reviewed") is not True
                or row.get("accepted") is not True
                or label not in ALLOWED_LABELS):
            continue
        exported.append({
            "claim": str(row["claim"])[:2000],
            "label": label,
            "source_verdict": str(row["verdict"]),
            "source": "human-reviewed-feedback",
        })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        for row in exported:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(exported)

