"""Retrieved text is business content written by people the engine does not control (an uploaded file, a crawled page). It is data. Nothing in it is an instruction, and it must never
steer the model that words an answer. Three defences, none of which relies on the model behaving:

  1. `injection_flags` finds text that addresses an AI ("ignore the previous instructions", "you are now ...", "reveal your system prompt", role markers). A flagged passage is shown
     to the person as a source, and may still be quoted verbatim, but it is never put into a model prompt.
  2. `fence` wraps whatever goes into a prompt in tags with a random name, so that a passage cannot close the tag and speak outside it; `clean` removes control characters and
     anything that looks like such a tag, and caps the length.
  3. Whatever the model returns is checked against the evidence (verify.py): an obeyed injection adds a link, a price or a promise that the evidence does not contain, and fails.
"""
from __future__ import annotations

import re
import secrets
import unicodedata

PATTERNS = {
    "override": r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your|the|these)\b[^.\n]{0,30}\b(instructions?|rules?|prompts?|guidelines?|constraints?)\b",
    "new_instructions": r"\b(new|updated|real|actual|secret)\s+(instructions?|rules?|system prompt)\b",
    "role_change": r"\byou\s+(are|will)\s+(now|henceforth)\b|\bact\s+as\s+(an?\s+)?(ai|assistant|bot|model|chatbot|dan|developer)\b|\bpretend\s+(to be|you are)\b",
    "reveal": r"\b(reveal|print|show|repeat|output|leak|tell me)\b[^.\n]{0,30}\b(system prompt|your (instructions|prompt|rules)|api[- ]?key|secret|password|credentials)\b",
    "role_marker": r"(^|\n)\s*(system|assistant|developer|user)\s*:|<\|?\s*(system|im_start|im_end|assistant)\s*\|?>|\[/?INST\]|###\s*(system|instruction)",
    "conceal": r"\b(do not|don't|never)\s+(tell|inform|mention to|reveal to)\s+(the\s+)?(user|customer|human)\b",
    "exfiltration": r"!\[[^\]]*\]\(\s*https?://|\bsend\b[^.\n]{0,40}\b(to|at)\s+https?://",
    "address_the_ai": r"\b(as an ai|language model|large language model|chatgpt|openai|anthropic)\b[^.\n]{0,30}\b(must|should|will|shall)\b",
}
COMPILED = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}
MAX_PASSAGE_CHARS = 1500


def injection_flags(text: str) -> list[str]:
    """The kinds of instruction-like text found (empty when none)."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    return [k for k, rx in COMPILED.items() if rx.search(t)]


def clean(text: str, limit: int = MAX_PASSAGE_CHARS) -> str:
    """Text made safe to place inside a fenced block: no control or invisible characters, no look-alike of a fence or role tag, a length cap."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = "".join(c for c in t if c in "\n\t " or unicodedata.category(c) not in ("Cc", "Cf"))
    t = re.sub(r"</?\s*(material|passages?|question|system|instructions?)[^>]*>", " ", t, flags=re.I)
    t = t.replace("```", "'''")
    return t[:limit]


def fence(label: str, body: str) -> tuple[str, str]:
    """(tag, fenced text): the tag has a random suffix, so the block cannot be closed from inside."""
    tag = f"{label}-{secrets.token_hex(6)}"
    return tag, f"<{tag}>\n{body}\n</{tag}>"
