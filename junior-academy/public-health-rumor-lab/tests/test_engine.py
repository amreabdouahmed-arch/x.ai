"""Strict tests for the public-health rumor lab engine."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from engine import check, load_kb  # noqa: E402


class TestRumorDetection(unittest.TestCase):
    def test_dialect_alias_matches_standard_arabic(self):
        self.assertEqual(
            check("المضاد الحيوي بيقتل الفيروسات").verdict,
            "rumor",
        )

    def test_multiple_negations_abstain(self):
        result = check("مش صح إن الكلور مش بيسبب ضرر")
        self.assertEqual(result.verdict, "unverified")
        self.assertIn("النفي المتعدد", result.title_ar)

    def test_knowledge_base_has_review_metadata(self):
        kb = load_kb()
        self.assertRegex(kb["meta"]["version"], r"^\d{4}\.\d{2}\.\d{2}$")
        self.assertIn("updated_at", kb["meta"])

    def test_bleach_is_high_harm_rumor(self):
        r = check("اشرب كلور عشان كورونا يروح")
        self.assertEqual(r.verdict, "rumor")
        self.assertEqual(r.harm, "high")
        self.assertTrue(r.sources)

    def test_antibiotics_virus_rumor(self):
        r = check("المضاد الحيوي يعالج كورونا")
        self.assertEqual(r.verdict, "rumor")
        self.assertIn("فيروس", r.explanation_ar)

    def test_vaccine_autism_rumor_en(self):
        r = check("vaccines cause autism")
        self.assertEqual(r.verdict, "rumor")
        self.assertEqual(r.matched_id, "vaccines-autism")

    def test_garlic_rumor(self):
        r = check("الثوم يعالج كورونا")
        self.assertEqual(r.verdict, "rumor")


class TestSupportedFacts(unittest.TestCase):
    def test_handwashing_supported(self):
        r = check("غسل اليدين بالصابون مهم للوقاية من العدوى")
        self.assertEqual(r.verdict, "supported")
        self.assertEqual(r.matched_id, "hand-hygiene")

    def test_ors_supported(self):
        r = check("محلول الجفاف الفموي يعوض السوائل في الإسهال")
        self.assertEqual(r.verdict, "supported")


class TestAbstain(unittest.TestCase):
    def test_empty_is_unverified(self):
        r = check("؟")
        self.assertEqual(r.verdict, "unverified")

    def test_unrelated_does_not_guess(self):
        r = check("أفضل لون لطلاء غرفة الانتظار في العيادة")
        self.assertEqual(r.verdict, "unverified")
        self.assertIsNone(r.matched_id)


class TestKbHygiene(unittest.TestCase):
    def test_every_entry_has_https_source(self):
        kb = load_kb()
        for entry in kb["entries"]:
            self.assertTrue(entry["sources"], entry["id"])
            for src in entry["sources"]:
                self.assertTrue(src["url"].startswith("https://"), src)

    def test_kinds_are_only_rumor_or_fact(self):
        kb = load_kb()
        self.assertEqual({e["kind"] for e in kb["entries"]}, {"rumor", "fact"})

    def test_result_always_has_academy_steps(self):
        r = check("xyz-not-a-claim-at-all-here")
        self.assertEqual(len(r.academy_steps), 5)


if __name__ == "__main__":
    unittest.main()
