"""Deterministic multi-signal public-health rumor checker.

This is a verifier, not an autonomous agent: it scores a claim against a
curated knowledge base and harm patterns, then explains every signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from normalize import fold_arabic, ngrams, tokens

ROOT = Path(__file__).resolve().parents[1]
KB_PATH = ROOT / "data" / "knowledge_base.json"


@dataclass
class Signal:
    name: str
    weight: float
    detail: str


@dataclass
class CheckResult:
    verdict: str
    confidence: float
    title_ar: str
    explanation_ar: str
    lesson_ar: str
    harm: str
    matched_id: str | None
    sources: list[dict]
    signals: list[Signal] = field(default_factory=list)
    academy_steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "title_ar": self.title_ar,
            "explanation_ar": self.explanation_ar,
            "lesson_ar": self.lesson_ar,
            "harm": self.harm,
            "matched_id": self.matched_id,
            "sources": self.sources,
            "signals": [{"name": s.name, "weight": s.weight, "detail": s.detail} for s in self.signals],
            "academy_steps": self.academy_steps,
            "disclaimer_ar": (
                "تعليمي فقط — ليس تشخيصاً ولا استشارة طبية. "
                "للقرار الصحي راجع وزارة الصحة أو منظمة الصحة العالمية."
            ),
        }


WHO_STEPS = [
    "اكتشاف الإشارة: هل الادعاء ينتشر كخبر علاجي قاطع؟",
    "التحقق: هل يوجد تطابق مع قاعدة معرفة موثّقة أو نمط ضرر معروف؟",
    "تقدير الخطر: هل التنفيذ يؤذي الجسد (شرب مطهر، تأخير لقاح)؟",
    "تصميم الرد: صنّف شائعة / مدعوم / غير كافٍ، واذكر المصدر.",
    "التوعية: لا تعِد الشائعة؛ انقل المصدر الرسمي.",
]


def load_kb(path: Path = KB_PATH) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _keyword_hits(text_folded: str, keywords: list[str]) -> list[str]:
    hits = []
    for kw in keywords:
        k = fold_arabic(kw)
        if k and k in text_folded:
            hits.append(kw)
    return hits


def _pattern_hit(text_folded: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if fold_arabic(p) in text_folded:
            return p
    return None


def _score_entry(claim: str, entry: dict) -> tuple[float, list[Signal]]:
    folded = fold_arabic(claim)
    claim_grams = ngrams(tokens(claim))
    patterns = entry.get("claim_patterns_ar", []) + entry.get("claim_patterns_en", [])
    pattern_grams: set[str] = set()
    for p in patterns:
        pattern_grams |= ngrams(tokens(p))

    jac = _jaccard(claim_grams, pattern_grams)
    hits = _keyword_hits(folded, entry.get("keywords", []))
    exact = _pattern_hit(folded, patterns)

    signals: list[Signal] = []
    score = 0.0
    if exact:
        signals.append(Signal("exact_pattern", 0.55, f"تطابق عبارة: {exact}"))
        score += 0.55
    if jac >= 0.12:
        signals.append(Signal("ngram_overlap", min(0.35, jac), f"تداخل نغرام={jac:.2f}"))
        score += min(0.35, jac)
    if hits:
        bump = min(0.28, 0.08 * len(hits))
        signals.append(Signal("keywords", bump, "كلمات مفتاحية: " + "، ".join(hits)))
        score += bump
    return min(score, 0.99), signals


def _flag_signals(claim: str, kb: dict) -> tuple[str, list[Signal]]:
    folded = fold_arabic(claim)
    harm = "none"
    signals: list[Signal] = []
    for flag in kb.get("red_flags", []):
        patterns = flag.get("ar", []) + flag.get("en", [])
        hit = _pattern_hit(folded, patterns)
        if hit:
            signals.append(Signal("red_flag", 0.4, f"{flag['id']}: {hit}"))
            if flag.get("harm") == "high":
                harm = "high"
            elif harm != "high":
                harm = "moderate"
    return harm, signals


def _multiple_negations(claim: str) -> bool:
    folded = fold_arabic(claim)
    markers = ("ليس", "لا", "لم", "لن", "مش", "موش", "مو")
    return sum(f" {marker} " in f" {folded} " for marker in markers) >= 2


def check(claim: str, kb: dict | None = None) -> CheckResult:
    kb = kb or load_kb()
    text = (claim or "").strip()
    if len(text) < 4:
        return CheckResult(
            verdict="unverified",
            confidence=0.0,
            title_ar="النص أقصر من أن يُفحص",
            explanation_ar="اكتب الادعاء بجملة كاملة حتى يمكن مطابقته.",
            lesson_ar="الشائعة تُفحص كنص واضح، لا ككلمة واحدة.",
            harm="none",
            matched_id=None,
            sources=[],
            academy_steps=WHO_STEPS,
        )

    ranked: list[tuple[float, dict, list[Signal]]] = []
    for entry in kb["entries"]:
        score, sigs = _score_entry(text, entry)
        ranked.append((score, entry, sigs))
    ranked.sort(key=lambda row: row[0], reverse=True)
    best_score, best, best_sigs = ranked[0]
    flag_harm, flag_sigs = _flag_signals(text, kb)
    if _multiple_negations(text):
        return CheckResult(
            verdict="unverified",
            confidence=0.2,
            title_ar="صياغة ملتبسة بسبب النفي المتعدد",
            explanation_ar="الصياغة تحتوي أكثر من نفي، وقد تعكس المعنى إذا قُرئت بسرعة. لا يمكن إصدار حكم آمن قبل إعادة كتابة الادعاء بوضوح.",
            lesson_ar="اكتب الادعاء بصيغة مباشرة: الشيء يسبب أو لا يسبب، بدل نفيين متداخلين.",
            harm=flag_harm,
            matched_id=None,
            sources=[],
            signals=flag_sigs,
            academy_steps=WHO_STEPS,
        )

    # Calibrated on the current reviewed knowledge-base set. Override with
    # RUMOR_MATCH_THRESHOLD during evaluation when a larger labeled set exists.
    import os
    threshold = float(os.environ.get("RUMOR_MATCH_THRESHOLD", "0.42"))
    if best_score >= threshold:
        kind = best["kind"]
        verdict = "rumor" if kind == "rumor" else "supported"
        harm = best.get("harm", "none")
        if flag_harm == "high":
            harm = "high"
        conf = best_score if verdict == "rumor" else max(best_score, 0.55)
        return CheckResult(
            verdict=verdict,
            confidence=min(conf, 0.97),
            title_ar=best["title_ar"],
            explanation_ar=best["verdict_ar"],
            lesson_ar=best["lesson_ar"],
            harm=harm,
            matched_id=best["id"],
            sources=best.get("sources", []),
            signals=best_sigs + flag_sigs,
            academy_steps=WHO_STEPS,
        )

    if flag_harm == "high":
        return CheckResult(
            verdict="rumor",
            confidence=0.8,
            title_ar="نمط ضرر جسدي مباشر",
            explanation_ar="النص يدعو لإدخال مادة سامة أو مطهّر إلى الجسم. هذا نمط شائعة خطرة حتى لو لم يطابق بطاقة كاملة في القاعدة.",
            lesson_ar="أي نص يطلب شرب أو حقن مطهر يُرفض فوراً ويُحوَّل لمصدر رسمي.",
            harm="high",
            matched_id=None,
            sources=[
                {
                    "label": "WHO Mythbusters",
                    "url": "https://www.who.int/emergencies/diseases/novel-coronavirus-2019/advice-for-public/myth-busters",
                }
            ],
            signals=flag_sigs,
            academy_steps=WHO_STEPS,
        )

    return CheckResult(
        verdict="unverified",
        confidence=round(best_score, 3),
        title_ar="غير كافٍ للحكم من القاعدة الحالية",
        explanation_ar=(
            "لا يوجد تطابق قوي مع الشائعات أو الحقائق الموثّقة هنا. "
            "لا تشارك الادعاء كخبر صحي، وراجعه عبر وزارة الصحة أو موقع المنظمة."
        ),
        lesson_ar="الامتناع أفضل من رقم واثق من غير مصدر. هذا درس الصحة العامة نفسه.",
        harm=flag_harm,
        matched_id=None,
        sources=[
            {
                "label": "WHO — Disinformation and public health",
                "url": "https://www.who.int/news-room/questions-and-answers/item/disinformation-and-public-health",
            }
        ],
        signals=best_sigs[:3] + flag_sigs,
        academy_steps=WHO_STEPS,
    )
