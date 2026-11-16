"""Light bilingual normalization for Junior Academy rumor matching."""

from __future__ import annotations

import re
import unicodedata

_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")
_NON_ALNUM = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)

AR_STOP = {
    "في", "من", "على", "عن", "إلى", "الى", "و", "أو", "او", "يا", "هو", "هي",
    "هذا", "هذه", "ذلك", "اللي", "أن", "ان", "إن", "ما", "لا", "نعم", "كل",
    "مع", "بعد", "قبل", "حتى", "كان", "يكون",
}
EN_STOP = {
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "are",
    "it", "this", "that", "with", "by", "from", "be", "as",
}

_DIALECT_ALIASES = {
    "بيسبب": "يسبب", "بتسبب": "تسبب", "تسببش": "لا تسبب",
    "مايسببش": "لا يسبب", "مش بيسبب": "لا يسبب",
    "بيعالج": "يعالج", "بتعالج": "تعالج", "بيعالجش": "لا يعالج",
    "ما بيعالجش": "لا يعالج", "مايعالجش": "لا يعالج",
    "بيحمي": "يحمي", "بيمنع": "يمنع", "بيقتل": "يقتل",
    "مو": "ليس", "موش": "ليس", "مهو": "ليس", "مش": "ليس",
    "مو صحيح": "ليس صحيح", "مش صحيح": "ليس صحيح",
    "يجيب": "يسبب", "يجيبش": "لا يسبب",
    "يخلي": "يجعل", "بيخلي": "يجعل", "يشفي": "يعالج",
    "شلون": "كيف", "وش": "ماذا", "شو": "ماذا", "ليش": "لماذا",
    "هالشي": "هذا الشيء", "هاد": "هذا", "هيدا": "هذا",
}


def fold_arabic(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _DIACRITICS.sub("", text)
    trans = str.maketrans(
        {
            "أ": "ا",
            "إ": "ا",
            "آ": "ا",
            "ى": "ي",
            "ة": "ه",
            "ؤ": "و",
            "ئ": "ي",
            "ٱ": "ا",
        }
    )
    folded = text.translate(trans).lower()
    for source, target in sorted(_DIALECT_ALIASES.items(), key=lambda item: -len(item[0])):
        folded = re.sub(rf"(?<!\w){re.escape(source)}(?!\w)", target, folded)
    return folded


def tokens(text: str) -> list[str]:
    folded = fold_arabic(text)
    parts = [p for p in _NON_ALNUM.split(folded) if p]
    out = []
    for p in parts:
        if p in AR_STOP or p in EN_STOP:
            continue
        if len(p) <= 1:
            continue
        out.append(p)
    return out


def ngrams(words: list[str], n: int = 2) -> set[str]:
    grams = set(words)
    for i in range(len(words) - n + 1):
        grams.add(" ".join(words[i : i + n]))
    return grams
