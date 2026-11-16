"""EpiGuard: a runnable public-health misinformation verifier.

Sense -> retrieve medical evidence -> judge -> explain -> remember.

  py agent/agent.py "هل المضاد الحيوي يعالج كورونا؟"
  py agent/agent.py --chat        # interactive REPL
  py agent/agent.py --ask "..."   # single question, JSON output

The verifier fetches what the network can prove, judges health rumors ONLY
from live accredited sources (no database, no canned text), and abstains
otherwise. When GEMINI_API_KEY is set, Gemini reads
the live evidence and delivers the verdict; the loop, tools, memory and
honesty rules are already the agent.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tools import (  # noqa: E402
    freebrain_available,
    gemini_available,
    gemini_judge_rumor,
    llm_judge_rumor,
    openrouter_available,
    stance_lean,
    verify_health_claim_online,
    fetch_url,
    web_search,
)

MEMORY = HERE / "memory.jsonl"

RUMOR_WORDS = ["لقاح", "تطعيم", "كلور", "مطهر", "مضاد", "كورونا", "كوفيد",
               "ثوم", "فيروس", "وباء", "vaccine", "bleach", "antibiotic",
               "covid", "virus", "garlic", "5g", "غسل", "يدين", "إسهال", "جفاف"]

# Broad routing vocabulary.  A claim should reach the medical evidence path
# even when it is not one of the pre-curated rumor examples.
HEALTH_CLAIM_WORDS = {
    "health", "healthy", "medical", "medicine", "medicines", "medication",
    "drug", "drugs", "doctor", "patient", "disease", "diseases", "illness",
    "symptom", "symptoms", "treatment", "therapy", "diagnosis", "clinical",
    "hospital", "clinic", "pregnant", "pregnancy", "baby", "child",
    "cancer", "tumor", "diabetes", "heart", "blood", "pressure", "lung",
    "kidney", "liver", "allergy", "asthma", "pain", "fever", "headache",
    "sleep", "nutrition", "vitamin", "mineral", "supplement", "coffee",
    "tea", "honey", "ginger", "lemon", "turmeric", "water", "sugar", "salt",
    "alcohol", "smoking", "obesity", "dehydration", "infection", "immune",
    "immunity", "exercise", "diet", "aspirin", "ibuprofen", "paracetamol",
    "antibiotic", "cause", "causes", "cure", "cures",
    "treat", "prevents", "prevent", "harm", "harmful", "safe", "safety",
}

_AR_NEGATION_MARKERS = (
    "مش", "موش", "مو", "ليس", "لا", "لم", "لن", "ما", "مفيش",
)
_MEDICATION_CHANGE_RE = re.compile(
    r"(?:إيقاف|ايقاف|أوقف|اوقف|وقف|توقف|أوقفش|اوقفش|وقفش|أغير|اغير|غير|"
    r"أضاعف|ضاعف|أقلل|قلل|أترك|اترك|أقطع|اقطع)"
    r".{0,45}(?:دواء|علاج|الضغط|السكر|المضاد|الجرعة|حبوب|دوائي)",
    re.IGNORECASE,
)
_URGENT_SYMPTOM_RE = re.compile(
    r"(?:ألم\s*(?:شديد\s*)?في\s*الصدر|وجع\s*الصدر|ضيق\s*التنفس|"
    r"صعوبة\s*التنفس|زرقة\s*الشفاه|فقدان\s*الوعي|إغماء|نزيف\s*شديد|"
    r"علامات\s*السكتة|تلعثم\s*مفاجئ|شلل\s*مفاجئ|"
    r"chest\s+pain|severe\s+bleeding|difficulty\s+breathing|"
    r"shortness\s+of\s+breath|loss\s+of\s+consciousness|"
    r"signs?\s+of\s+(?:a\s+)?stroke)",
    re.IGNORECASE,
)


def _looks_like_health_claim(text: str) -> bool:
    """Route broad English medical claims without routing every web question."""
    if re.search(r"[\u0600-\u06FF]", text):
        return True
    words = set(re.findall(r"[a-z]+", text.lower()))
    return bool(words & HEALTH_CLAIM_WORDS)


def _has_ambiguous_negation(text: str) -> bool:
    """Reject nested negation before online evidence can invert its meaning."""
    folded = re.sub(r"[؟?!.,،:؛()]", " ", text)
    words = folded.split()
    return sum(word in _AR_NEGATION_MARKERS for word in words) >= 2


def _unsafe_medication_change(text: str) -> bool:
    if re.search(r"(?:لا|عدم)\s+(?:توقف|إيقاف|ايقاف|تغيير|تغير)", text):
        return False
    return bool(_MEDICATION_CHANGE_RE.search(text))


def _urgent_symptom(text: str) -> bool:
    """Recognize a small set of emergency red flags before online lookup."""
    if any(marker in text for marker in ("لا ", "ليس ", "مش ", "موش ", "لا يوجد")):
        return False
    return bool(_URGENT_SYMPTOM_RE.search(text))


def ask(text: str, web_grounding: bool = True) -> dict:
    """One agent turn. Always returns a dict; never raises to the caller.

    web_grounding=True attaches live web evidence by default. It never
    overrides a double-checked derivation or a health verdict from accredited
    sources.
    """
    text = (text or "").strip()
    if len(text) < 2:
        return {"mode": "clarify",
                "answer": "اكتب سؤالك كاملاً عن شائعة صحية أو معلومة طبية، أو أرسل رابطًا طبيًا.",
                "verification": "input too short; nothing attempted."}
    try:
        if _has_ambiguous_negation(text):
            return _remember(text, {
                "mode": "rumor",
                "verdict": "unverified",
                "confidence": 0.2,
                "decided_by": "⛔ بوابة أمان: نفي ملتبس",
                "answer": (
                    "⚠️ لا أستطيع إصدار حكم آمن لأن الجملة تحتوي أكثر من نفي. "
                    "أعد صياغتها بجملة مباشرة، مثل: «هل يسبب الشيء ضررًا؟»."
                ),
                "sources": [],
                "online_hits": 0,
                "online_verdict": None,
                "category": "needs_human_review",
                "verification": "ambiguous nested negation rejected before web search.",
            })
        if _unsafe_medication_change(text):
            return _remember(text, {
                "mode": "rumor",
                "verdict": "unverified",
                "confidence": 0.0,
                "decided_by": "⛔ بوابة أمان دوائية",
                "answer": (
                    "⚠️ لا توقف أو تغيّر جرعة أي دواء بناءً على هذا الرد. "
                    "تحسن الأعراض لا يعني أن إيقاف العلاج آمن؛ راجع طبيبك أو "
                    "الصيدلي قبل أي تغيير، وإذا ظهرت أعراض خطيرة اطلب رعاية عاجلة."
                ),
                "sources": [],
                "online_hits": 0,
                "online_verdict": None,
                "category": "unsafe_advice",
                "verification": "unsafe medication-change advice rejected before web search.",
            })
        if _urgent_symptom(text):
            return _remember(text, {
                "mode": "urgent_safety",
                "verdict": "unverified",
                "confidence": 0.0,
                "decided_by": "⛔ بوابة سلامة للحالات الطارئة",
                "answer": (
                    "🚨 هذه الأعراض قد تكون حالة طارئة. لا تنتظر نتيجة بحث أو "
                    "تشخيصًا من الإنترنت: اتصل بخدمات الطوارئ المحلية فورًا أو "
                    "اذهب إلى أقرب قسم طوارئ، خصوصًا إذا كانت الأعراض شديدة أو "
                    "مفاجئة أو تتفاقم."
                ),
                "sources": [],
                "online_hits": 0,
                "online_verdict": None,
                "category": "urgent_care",
                "verification": "emergency red-flag guidance issued before web search.",
            })
        folded = text
        if any(w in folded.lower() for w in RUMOR_WORDS) or _looks_like_health_claim(text):
            # PURE WEB: no local database, no canned answers. The verdict
            # comes only from live accredited sources. If the web is
            # unreachable, the agent says so instead of reciting a file.
            try:
                online = verify_health_claim_online(text)
            except Exception as exc:
                online = {"hits": [], "pages_checked": 0,
                          "search_note": f"online check unavailable: {exc}",
                          "corroborated": False, "online_verdict": None,
                          "stance_tally": {}, "keywords": []}
            out = _merge_rumor_answer(None, online)
            # BRAIN (advisor only): the language mind reads the live evidence,
            # but the verdict stays with the grounded stance tally. If they
            # agree, the brain's explanation is adopted; if not, its take is
            # shown as a second opinion and the grounded verdict stands.
            if ((gemini_available() or openrouter_available()
                    or freebrain_available())
                    and out.get("online_hits")):
                try:
                    judge = llm_judge_rumor(text, [
                        {"label": s["label"], "url": s["url"],
                         "snippet": next((h["snippet"] for h in
                                          (online.get("hits") or [])
                                          if h["url"] == s["url"]), "")}
                        for s in out.get("sources", [])])
                    if "Gemini" in judge["decided_by"]:
                        brain_tag = "gemini"
                    elif "OpenRouter" in judge["decided_by"]:
                        brain_tag = "openrouter"
                    elif "HuggingFace" in judge["decided_by"]:
                        brain_tag = "hf"
                    else:
                        brain_tag = "free-llm"
                    # Never let model output override the deterministic,
                    # source-grounded verdict. A prompt injection or model
                    # disagreement may affect explanation, not the verdict.
                    if judge["verdict"] == out["verdict"]:
                        out["confidence"] = judge["confidence"]
                        out["decided_by"] = judge["decided_by"]
                        out["answer"] = (out["answer"].split("تنبيه:")[0].rstrip() +
                                         "\n\n🧠 شرح الحكم بعد قراءة الأدلة:\n" +
                                         judge["explanation"] +
                                         "\n\nتنبيه: محتوى تعليمي، ليس استشارة طبية — "
                                         "راجع وزارة الصحة أو WHO للقرار الصحي.")
                        out["verification"] += f" | brain={brain_tag}:supporting"
                    else:
                        out["verification"] += (
                            f" | brain={brain_tag}:disagreement-kept-grounded-verdict"
                        )
                except Exception:
                    pass  # silent fallback: stance verdict stands, no ugly notes
            # rumor answers already carry live evidence; extra DDG grounding
            # would only add seconds, so ground only evidence-free answers
            if web_grounding and not out.get("online_hits"):
                _attach_search(out, text)
            return _remember(text, out)

        urls = re.findall(r"https?://\S+", text)
        if urls:
            url = urls[0].rstrip(").,")
            r = fetch_url(url)
            if r["ok"]:
                return _remember(text, {"mode": "web", "answer":
                                        f"Fetched {r['url']} (HTTP {r['status']}, {r['bytes']} bytes).",
                                        "citation": r["url"],
                                        "verification": f"live fetch, snippet head: {r['snippet'][:200]!r}"})
            return _remember(text, {"mode": "web-failed", "answer":
                                    f"لم أستطع الجلب ({r['error']}). لن أخمن المحتوى من الذاكرة.",
                                    "verification": "honest fetch failure; no content fabricated."})

        return _remember(text, {"mode": "clarify",
                                "answer": "ابعت شائعة أو ادعاءً متعلقًا بالصحة العامة، "
                                          "أو الصق رابطًا طبيًا للتحقق منه.",
                                "verification": "no tool matched; asked for clarification."})

    except Exception as exc:
        return _remember(text, {"mode": "error",
                                "answer": f"تعذر إكمال التحقق الصحي: {exc}",
                                "verification": "explicit error from medical verification path."})

def _merge_rumor_answer(_unused, online: dict) -> dict:
    """Pure-web rumor verdict. No database, no canned text.

    The verdict comes ONLY from live accredited sources (stance tally).
    Health-claim routing got us here; everything below is fetched, not filed.
    """
    online = online or {}
    hits = online.get("hits", [])
    web_verdict = online.get("online_verdict")
    tally = online.get("stance_tally") or {}
    kws = (online.get("keywords") or [])[:6]
    rm = sum(h.get("margin", 0) for h in hits if h.get("stance") == "refutes")
    sm = sum(h.get("margin", 0) for h in hits if h.get("stance") == "supports")
    lean_pct, lean_dir = stance_lean(rm, sm)

    if not hits and (online.get("pages_checked", 0) == 0):
        return {"mode": "rumor", "verdict": "unverified", "confidence": 0.0,
                "decided_by": "⛔ لا إنترنت",
                "answer": ("⛔ لا يوجد اتصال بالمصادر الطبية المعتمدة الآن.\n"
                           "لن أخمن ولن أجيب من نصوص جاهزة — تحقق من الاتصال وحاول مجدداً."),
                "sources": [], "online_hits": 0,
                "online_verdict": None,
                "category": "needs_human_review",
                "verification": "no live pages reachable; refused to answer from memory."}

    evidence_grade = "غير كافٍ للحكم"
    if web_verdict in ("rumor", "supported"):
        final_verdict = web_verdict
        decided_by = "🌐 الإنترنت الحي (مصادر معتمدة)"
        trusted_domains = online.get("trusted_domains") or []
        decisive_sources = int(online.get("decisive_sources", 0))
        if len(trusted_domains) >= 2 and decisive_sources >= 2:
            confidence = 0.85
            evidence_grade = "قوي: أكثر من مصدر معتمد مستقل"
        else:
            confidence = 0.68
            evidence_grade = "متوسط: يحتاج تأكيدًا من مصدر طبي مستقل إضافي"
    else:
        final_verdict, decided_by, confidence = "unverified", "🌐 الإنترنت الحي (أدلة غير حاسمة)", 0.4

    verdict_ar = {"rumor": "❌ شائعة — معلومة مضللة",
                  "supported": "✅ معلومة صحيحة ومدعومة",
                  "unverified": "⚠️ غير مؤكد — لا يوجد دليل كافٍ"}[final_verdict]
    thinking = ["🧠 تحليل الوكيل (بحث حي، بلا قاعدة بيانات):",
                f"• كلمات البحث: {', '.join(kws) or '—'}",
                f"• فحصت {online.get('pages_checked', 0)} صفحات طبية معتمدة: "
                f"تفنيد ×{tally.get('refutes', 0)}، تأييد ×{tally.get('supports', 0)}",
                f"• جودة الدليل: {evidence_grade}",
                f"• ميل الأدلة: {lean_pct}% — {lean_dir}"]
    if hits:
        thinking.append(f"• أقوى دليل حي: {hits[0]['label']}")
    thinking.append(f"• الحكم من: {decided_by}")

    parts = ["\n".join(thinking), "", verdict_ar, ""]
    if web_verdict is not None:
        parts += [f"موقف المصادر المعتمدة: "
                  f"{'تفنّد الادعاء' if web_verdict == 'rumor' else 'تؤيد الادعاء'} "
                  f"(تفنيد ×{tally.get('refutes', 0)}، تأييد ×{tally.get('supports', 0)})", ""]
    sources = []
    if hits:
        parts += ["🌐 بالنص من المصادر المعتمدة:"]
        for h in hits[:3]:
            tag = "" if h.get("trusted", True) else " (مصدر غير معتمد — للاستئناس)"
            stance = {"refutes": " [يفنّد]", "supports": " [يؤيد]"}.get(h.get("stance"), "")
            parts.append(f"• {h['label']}{stance}{tag}: «{h['snippet'][:180]}…»")
            sources.append({"label": h["label"], "url": h["url"]})
        if final_verdict == "unverified":
            parts.append(f"لا يوجد حكم قاطع، لكن الأدلة تميل بنسبة {lean_pct}% ({lean_dir}) — "
                         "هذه أقرب ما تقوله المصادر المعتمدة عن الموضوع.")
    else:
        parts += ["🌐 لم أجد أي تطابق في المصادر المعتمدة — لا أستطيع الحكم."]
    parts += ["", "تنبيه: محتوى تعليمي، ليس استشارة طبية — راجع وزارة الصحة أو WHO للقرار الصحي."]
    return {"mode": "rumor", "verdict": final_verdict, "confidence": confidence,
            "decided_by": decided_by,
            "answer": "\n".join(parts), "sources": sources,
            "online_hits": len(hits),
            "online_verdict": web_verdict,
            "category": (
                "misinformation" if final_verdict == "rumor"
                else "supported" if final_verdict == "supported"
                else "needs_human_review"
            ),
            "pages_checked": online.get("pages_checked", 0),
            "lean_pct": lean_pct, "lean_dir": lean_dir,
            "evidence_grade": evidence_grade if web_verdict else "غير كافٍ للحكم",
            "trusted_domains": online.get("trusted_domains", []),
            "verification": (f"pure web | تفنيد ×{tally.get('refutes', 0)} "
                             f"تأييد ×{tally.get('supports', 0)} | {len(hits)} أدلة")}


def _attach_search(result: dict, text: str) -> None:
    """Grounding pass: live search attached as extra evidence, in place."""
    try:
        s = web_search(text[:200])
    except Exception:
        return
    if s["ok"]:
        result["web_evidence"] = s["answer"]
        if s.get("url"):
            result["citation"] = s["url"]
        result["verification"] = (result.get("verification", "") +
                                  f" | web: {s['verification']}")
    else:
        result["web_note"] = f"live search: {s.get('error')}"


def _remember(text: str, result: dict) -> dict:
    result = {"input": text, **result}
    try:
        with open(MEMORY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"when": datetime.datetime.now().isoformat(timespec="seconds"),
                                 **result}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return result


def main(argv=None) -> int:
    try:  # Windows consoles default to cp1252, which cannot print Arabic
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="EpiGuard public-health misinformation verifier")
    ap.add_argument("question", nargs="?", help="question to answer")
    ap.add_argument("--ask", dest="question2", help="same as positional")
    ap.add_argument("--chat", action="store_true", help="interactive REPL")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    ap.add_argument("--no-web", action="store_true",
                    help="disable live web grounding for this run")
    args = ap.parse_args(argv)
    q = args.question2 or args.question
    if args.chat or not q:
        print("EpiGuard agent — اكتب سؤالك (exit للخروج)")
        while True:
            try:
                line = input("أنت> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if line.lower() in ("exit", "quit", "خروج"):
                break
            out = ask(line, web_grounding=not args.no_web)
            print("الوكيل>", out["answer"])
            print(f"   [mode={out['mode']}] {out.get('verification', '')}")
        return 0
    out = ask(q, web_grounding=not args.no_web)
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out["answer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
