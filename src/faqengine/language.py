"""A rough guess at what language a knowledge base is written in, so that a mismatch with the embedding model is flagged before training
instead of showing up as mysteriously poor results. No dependencies, no network: it counts letters by alphabet and common words by language.

The default model (all-MiniLM-L6-v2) was trained on English. It still does something with other Latin-script languages, but a model trained on
many languages (multilingual-e5-small) does far better, and for other alphabets the English model is close to useless.
"""
from __future__ import annotations

import re
import unicodedata

SCRIPTS = [  # (name, first code point, last code point, languages it is mostly used for)
    ("Devanagari", 0x0900, 0x097F, "Hindi or Marathi"), ("Bengali", 0x0980, 0x09FF, "Bengali"), ("Gurmukhi", 0x0A00, 0x0A7F, "Punjabi"),
    ("Gujarati", 0x0A80, 0x0AFF, "Gujarati"), ("Tamil", 0x0B80, 0x0BFF, "Tamil"), ("Telugu", 0x0C00, 0x0C7F, "Telugu"),
    ("Kannada", 0x0C80, 0x0CFF, "Kannada"), ("Malayalam", 0x0D00, 0x0D7F, "Malayalam"), ("Arabic", 0x0600, 0x06FF, "Arabic, Urdu or Persian"),
    ("Hebrew", 0x0590, 0x05FF, "Hebrew"), ("Thai", 0x0E00, 0x0E7F, "Thai"), ("Hiragana/Katakana", 0x3040, 0x30FF, "Japanese"),
    ("CJK", 0x4E00, 0x9FFF, "Chinese (or Japanese)"), ("Hangul", 0xAC00, 0xD7AF, "Korean"), ("Cyrillic", 0x0400, 0x04FF, "Russian or another Cyrillic language"),
    ("Greek", 0x0370, 0x03FF, "Greek"),
]

WORDS = {  # very common words, chosen to be rare in the other languages' text
    "English": "the is are and of to a in for you your we our how what do can i it on be with that this or if at by from my not will have does which".split(),
    "Spanish": "de la el los las que en un una por con para es del se su como más cómo qué puedo tiene hay y son al lo".split(),
    "French": "le la les des du de et est un une pour que qui dans en vous nous je ce pas sur au aux comment quels puis est-ce".split(),
    "German": "der die das und ist nicht ein eine zu den mit für auf wie ich sie wir kann haben werden von im dem".split(),
    "Portuguese": "de que não um uma para com os as do da em é você como posso tem ou mais são ao".split(),
    "Italian": "il lo la gli le di che è un una per con non sono come posso del della nel dei delle".split(),
    "Dutch": "de het een en van ik je dat niet is voor met op te hoe kan zijn wat".split(),
    "Indonesian": "yang dan di ke dari untuk dengan tidak ini itu apakah bagaimana saya anda ada bisa atau pada".split(),
    "Hinglish (Hindi in Latin letters)": "hai hain kya kaise aap mera meri nahi nahin karna kare hum tum kab kahan kyun kitna kitne chahiye milega hota hoga kaun aur bhi mein ka ki ke ko".split(),
}
SETS = {name: set(words) for name, words in WORDS.items()}


def detect(texts: list[str], sample_chars: int = 60_000) -> dict:
    """{"language": "English", "english": True, "evidence": "..."}. Without enough text to tell, it says English (the default) and says it was not sure."""
    text = " ".join(texts)[:sample_chars]
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 20:
        return {"language": "English", "english": True, "sure": False, "evidence": "too little text to tell"}
    counts: dict[str, int] = {}
    for ch in letters:
        cp = ord(ch)
        for name, lo, hi, _ in SCRIPTS:
            if lo <= cp <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    if counts:
        name, n = max(counts.items(), key=lambda kv: kv[1])
        if n / len(letters) >= 0.3:
            label = next(who for nm, _, _, who in SCRIPTS if nm == name)
            return {"language": f"{label} ({name} script)", "english": False, "sure": True, "evidence": f"{100 * n / len(letters):.0f}% of the letters are {name}"}
    words = re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)?", unicodedata.normalize("NFC", text.lower()))
    if len(words) < 15:
        return {"language": "English", "english": True, "sure": False, "evidence": "too little text to tell"}
    score = {name: sum(w in common for w in words) / len(words) for name, common in SETS.items()}
    best = max(score, key=score.get)
    if best != "English" and score[best] >= 0.12 and score[best] >= 1.5 * score["English"]:
        return {"language": best, "english": False, "sure": True, "evidence": f"{100 * score[best]:.0f}% of the words are common {best.split(' (')[0]} words, {100 * score['English']:.0f}% English"}
    return {"language": "English", "english": True, "sure": score["English"] >= 0.12, "evidence": f"{100 * score['English']:.0f}% of the words are common English words"}


def detect_kb(kb) -> dict:
    return detect([f"{e.question} {e.answer}" for e in kb.entries[:600]])


def advice(found: dict, base_languages: str) -> str | None:
    """What to tell a person when the text and the model do not match; None when they do (or when it could not be told)."""
    if found["english"] or not found.get("sure") or base_languages == "multilingual":
        return None
    return (f"This looks like {found['language']} ({found['evidence']}), but the model is English-only, so it will match poorly. "
            "Choose the multilingual model (a one-time download of about 470 MB) and train again.")
