import json
import tempfile
import unittest
from pathlib import Path

from feedback_learning import export_reviewed_training_data


class TestFeedbackLearning(unittest.TestCase):
    def test_exports_only_reviewed_accepted_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "feedback.jsonl"
            output = root / "training.jsonl"
            rows = [
                {"claim": "ادعاء موثق", "verdict": "rumor",
                 "reviewed": True, "accepted": True, "label": "rumor"},
                {"claim": "رأي مستخدم فقط", "verdict": "supported",
                 "reviewed": False, "accepted": True, "label": "supported"},
                {"claim": "تسمية غير صالحة", "verdict": "rumor",
                 "reviewed": True, "accepted": True, "label": "medical-advice"},
            ]
            source.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
                encoding="utf-8",
            )

            count = export_reviewed_training_data(source, output)

            self.assertEqual(count, 1)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["label"], "rumor")
            self.assertEqual(exported["source"], "human-reviewed-feedback")


if __name__ == "__main__":
    unittest.main()
