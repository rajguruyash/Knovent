"""What a business is shown before it agrees to send data to a third-party AI provider, and what the agreement is bound to. Provider-specific details are DATA that a person verified, never
promises written here: a provider's data-handling terms are the provider's, they change, and this service has not verified any of them unless an entry says it did, with the address, the
service tier, the date and every item of docs/provider-verification.md. Nothing is inferred and nothing is filled in: an item that is missing, or says it is unknown or not stated, leaves the
provider UNVERIFIED, and an unverified provider can neither be agreed to nor used (the service checks this on every call, not only when the agreement is made).

The version of a disclosure is the base version plus a fingerprint of the text itself, so editing a provider's entry (or the common text) makes every earlier agreement stop applying until the
business reads the new text and agrees again. Standard library only.

    python -m faqengine.hosted.disclosure       prints where every provider stands and what is missing
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date

BASE_VERSION = "2026-11-02"

COMMON = {
    "what_is_sent": ["the question a customer asks", "the passages of your own knowledge (FAQ answers, document and spreadsheet rows, web pages) that were retrieved for that question and are needed to answer it"],
    "not_sent": ["your whole knowledge base", "your other businesses' data", "your API keys or any credential", "the identity of the customer unless they wrote it in the question"],
    "third_party": "The provider is a separate company. Once the text reaches it, how it is stored, how long, who can read it and whether it is used to improve models are governed by the provider's own terms, not by this service.",
    "deterministic": "Without this agreement, and in deterministic mode, your questions and knowledge are answered inside this service and nothing is sent to an AI inference provider.",
    "revoke": "You can withdraw this agreement at any time, as an owner or admin of the business. It takes effect on the very next question: no further text is sent to that provider. Text already sent cannot be recalled by this service.",
    "separate": "Agreeing for one provider is not agreeing for another, and agreeing to an earlier version of this text is not agreeing to a later one.",
}

# What a verification must establish (docs/provider-verification.md). A value is an answer written by the person who read the provider's terms; "unknown" and "not stated" are not answers.
REQUIRED = ("terms_url", "service_tier", "retention", "training_use", "abuse_monitoring", "contractual_commitments", "verified_on")
OPTIONAL = ("regional_processing", "verified_by")
LABELS = {
    "terms_url": "the address of the provider's official terms", "service_tier": "the service tier these terms apply to", "retention": "how long the provider keeps submitted text",
    "training_use": "whether submitted text is used to train or improve models", "abuse_monitoring": "what the provider does to monitor for abuse (who can read submitted text)",
    "contractual_commitments": "the contractual commitments that apply (data processing terms, what the business can rely on)", "verified_on": "the date the terms were read",
    "regional_processing": "where the text is processed", "verified_by": "who read the terms",
}
UNKNOWN = re.compile(r"^\s*(?:(?:unknown|unclear|tbd|todo|n/?a|none found|not (?:stated|specified|checked|verified|found|known|published|available)|no information)\b|\?+\s*$)|^\s*$", re.I)

_BLANK = {k: None for k in (*REQUIRED, *OPTIONAL)}
# provider key -> what is known about it. Every entry is unverified until a person fills in every REQUIRED item from the provider's own terms.
PROVIDERS: dict[str, dict] = {
    "cloudflare": {"label": "Cloudflare Workers AI", "role": "platform-managed", **_BLANK},
    "openrouter": {"label": "OpenRouter (which forwards to the model's own host)", "role": "platform-managed or your own key", **_BLANK},
    "gemini": {"label": "Google Gemini API (your own key)", "role": "customer-managed (BYOK)", **_BLANK},
    "openai": {"label": "OpenAI API (your own key)", "role": "customer-managed (BYOK)", **_BLANK},
    "anthropic": {"label": "Anthropic API (your own key)", "role": "customer-managed (BYOK)", **_BLANK},
    "openai_compatible": {"label": "The server you configured (OpenAI-compatible address)", "role": "customer-managed", **_BLANK},
}


def providers() -> dict[str, dict]:
    """The provider entries, from AI_DISCLOSURE_FILE (a JSON object of the same shape) when set, otherwise the defaults above."""
    path = os.environ.get("AI_DISCLOSURE_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, dict)}
    return PROVIDERS


def known(provider: str) -> bool:
    return provider in providers()


def _answered(value: object) -> bool:
    return isinstance(value, str) and not UNKNOWN.search(value)


def verification(provider: str, today: date | None = None) -> dict:
    """{status: verified | unverified, missing: [item, ...], problems: [...]}: verified only when every REQUIRED item is an answer, the address is https and the date is a real day that is not in the future."""
    p = providers()[provider]
    missing = [k for k in REQUIRED if not _answered(p.get(k))]
    problems = []
    url = p.get("terms_url")
    if _answered(url) and not str(url).strip().lower().startswith("https://"):
        problems.append("terms_url must be an https address")
    on = p.get("verified_on")
    if _answered(on):
        try:
            day = date.fromisoformat(str(on).strip())
            if day > (today or date.today()):
                problems.append("verified_on is in the future")
        except ValueError:
            problems.append("verified_on must be a date written 2026-12-31")
    return {"status": "unverified" if missing or problems else "verified", "missing": missing, "problems": problems}


def verified(provider: str) -> bool:
    return known(provider) and verification(provider)["status"] == "verified"


def text(provider: str) -> str:
    """The disclosure as the business reads it, for one provider."""
    p = providers().get(provider)
    if p is None:
        raise KeyError(provider)
    v = verification(provider)
    if v["status"] == "verified":
        facts = [f"  - {LABELS[k][0].upper() + LABELS[k][1:]}: {str(p[k]).strip()}" for k in ("service_tier", "retention", "training_use", "abuse_monitoring", "contractual_commitments", "regional_processing") if _answered(p.get(k))]
        terms = [f"This service read the provider's terms at {str(p['terms_url']).strip()} on {str(p['verified_on']).strip()}" + (f" ({str(p['verified_by']).strip()})" if _answered(p.get("verified_by")) else "") +
                 ". What it recorded, in the words of the person who read them:", *facts, "Terms change; if the provider's change, this text is checked again and, if it differs, you are asked again."]
    else:
        gaps = [LABELS[k] for k in v["missing"]] + v["problems"]
        terms = ["This service has NOT verified this provider's terms, retention or whether it uses submitted text for training. Read the provider's own terms before agreeing; nothing here is a promise about them.",
                 "Not yet established: " + "; ".join(gaps) + ".", "Agreement is not available for this provider until its terms have been verified."]
    lines: list[str] = [f"Sending your data to {p['label']} ({p.get('role', 'third party')})", "", "What may be sent:", *[f"  - {x}" for x in COMMON["what_is_sent"]], "", "What is never sent:",
                        *[f"  - {x}" for x in COMMON["not_sent"]], "", str(COMMON["third_party"]), *terms, "", str(COMMON["deterministic"]), str(COMMON["revoke"]), str(COMMON["separate"])]
    return "\n".join(lines)


def sha256(provider: str) -> str:
    return hashlib.sha256(text(provider).encode()).hexdigest()


def current_version(provider: str) -> str:
    """BASE_VERSION plus the first eight characters of the text's fingerprint: a change to the text is a new version."""
    return f"{BASE_VERSION}.{sha256(provider)[:8]}"


def describe(provider: str) -> dict:
    v = verification(provider)
    return {"provider": provider, "label": providers()[provider]["label"], "version": current_version(provider), "sha256": sha256(provider), "text": text(provider), "verified": v["status"] == "verified",
            "verification": v, "consentable": v["status"] == "verified", "data_categories": ["question", "evidence_passages"]}


def main() -> None:
    rows = [(k, verification(k)) for k in providers()]
    print(f"{'provider':<20} {'status':<11} not yet established")
    for k, v in rows:
        print(f"{k:<20} {v['status']:<11} {', '.join(v['missing'] + v['problems']) or '-'}")
    print(f"\n{sum(v['status'] == 'verified' for _, v in rows)} of {len(rows)} providers verified. Only a verified provider can be agreed to or used.")


if __name__ == "__main__":
    main()
