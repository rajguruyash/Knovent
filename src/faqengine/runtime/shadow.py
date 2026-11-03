"""Shadow comparison: the existing path (answer.respond) and the pipeline (pipeline.py, deterministic mode) are run on the SAME request and compared. Used by the hosted service and by the Studio,
so one definition of "the same answer" serves both. Nothing here changes what a person is shown: the caller returns one response and records only the differences, as short phrases and ids,
never document text. Standard library only."""
from __future__ import annotations

MODES = ("off", "shadow", "on")  # off: the existing path answers; shadow: it still answers and the pipeline is run beside it and compared; on: the pipeline answers (the existing path is the way back)


def mode_from(value: str | None) -> str:
    """The pipeline setting from an environment value ('' or missing is off); anything else that is not a mode is an error, so a typo cannot silently mean 'off' or 'on'."""
    mode = (value or "off").strip().lower() or "off"
    if mode not in MODES:
        raise ValueError(f"the pipeline setting must be one of {', '.join(MODES)} (got {value!r})")
    return mode


def legacy_execution(result: dict, why: str | None = None, provider: str | None = None, reason: str | None = None) -> dict:
    """What the existing path did, in the shape the pipeline reports (`execution`), so a reader of either sees the same fields: which path, which mode, who wrote the answer, and why not a model.
    `why` is a consent failure that kept a configured model out; `reason` says why the pipeline did not answer."""
    part = result.get("llm") or {}
    used = bool(result.get("used_llm"))
    fb = (result.get("fallback") or {}).get("reason")
    failure = why or (None if not fb else "reasoning:failed_verification" if fb == "failed_verification" else f"provider:{fb}")
    gen = "model" if used else "deterministic_no_consent" if why else "deterministic_fallback" if fb else "deterministic"
    return {"path": "legacy", "mode": "byok" if (provider or used) else "deterministic", "provider": provider or part.get("provider"), "model": part.get("model"), "inference_attempted": bool(part.get("attempted")),
            "inference_succeeded": used, "fallback": bool(fb), "generated_by": gen, "failure_kind": failure, **({"pipeline_note": reason} if reason else {})}


def _ids(result: dict) -> list[str]:
    return sorted(str(s.get("id")) for s in result.get("sources") or [])


def _claims(result: dict, strict: bool = False) -> list[str]:
    """The statements an answer makes. A conflict has no claims of its own in the existing result: what it says is each side's statement, and the pipeline lists them as claims, so both are read the same way
    (unless `strict`)."""
    own = [c.get("text", "") for c in result.get("claims") or []]
    if strict or own or result.get("outcome") != "conflict":
        return own
    return [s.get("text", "") for cf in result.get("conflicts") or [] for s in cf.get("sources", [])]


def _inactive(result: dict) -> list[str]:
    return sorted(str(m.get("entry_id")) for m in ((result.get("retrieval") or {}).get("diagnostics") or {}).get("inactive_matches", []))


def _candidates(result: dict) -> list[str]:
    return [str(c.get("entry_id")) for c in (result.get("retrieval") or {}).get("candidates", [])]


def compare(legacy: dict, new: dict, strict: bool = False) -> list[str]:
    """What differs between the two results, as phrases (empty when they agree). Compared: the outcome, the answer, what it cites, the claims, the conflicts, abstention, the structured (computed)
    results, the candidates retrieved (the same business's rows, the same order; a second, targeted retrieval may only add to the end) and the material left out as not current (the same freshness).
    `strict` switches off the two allowances above (extra candidates after a second retrieval; a conflict's sides read as claims): what the comparison finds without them, so that how often they fire can be counted."""
    out: list[str] = []
    if legacy.get("outcome") != new.get("outcome"):
        out.append(f"outcome: {legacy.get('outcome')} vs {new.get('outcome')}")
    for key in ("answer", "partial_answer"):
        if (legacy.get(key) or None) != (new.get(key) or None):
            out.append(f"{key} differs")
    if _ids(legacy) != _ids(new):
        out.append("cited sources differ")
    if _claims(legacy, strict) != _claims(new, strict):
        out.append("claims differ")
    if len(legacy.get("conflicts") or []) != len(new.get("conflicts") or []):
        out.append("conflicts differ")
    if bool(legacy.get("unanswered_parts")) != bool(new.get("unanswered_parts")) or (legacy.get("unanswered_parts") or []) != (new.get("unanswered_parts") or []):
        out.append("unanswered parts differ")
    if (legacy.get("computed") or None) != (new.get("computed") or None):
        out.append("structured results differ")
    before, after = _candidates(legacy), _candidates(new)
    second_pass = int((new.get("execution") or {}).get("retrieval_passes") or 1) > 1
    if after != before and not (second_pass and not strict and after[:len(before)] == before):  # the one targeted retrieval may ADD candidates after the first search's, in the same order; nothing else may differ
        out.append("retrieved candidates differ")
    if _inactive(legacy) != _inactive(new):
        out.append("freshness differs: not-current material differs")
    return out


# What a difference is ABOUT, so that a record can be read at a glance and counted. This only names the phrases `compare` produced; it decides nothing about what counts as a difference.
CATEGORIES = ("answer", "claims", "citations", "evidence", "abstention", "conflict", "structured", "tenant", "exception")
ABSTAINS = ("insufficient", "clarification")


def categories(differences: list[str]) -> list[str]:
    """The kinds of difference in a list from `compare`: answer, claims, citations, evidence (candidates retrieved or what was left out as not current), abstention (answered on one path and not on
    the other, or what was left unanswered), conflict, structured (a computed result), tenant. An outcome that changed between two answers is an `answer` difference."""
    out: list[str] = []
    for d in differences:
        if d.startswith("outcome:"):
            sides = d.removeprefix("outcome:").split(" vs ")
            kind = "abstention" if len(sides) == 2 and (sides[0].strip() in ABSTAINS) != (sides[1].strip() in ABSTAINS) else "answer"
        elif d.startswith(("answer differs", "partial_answer differs")):
            kind = "answer"
        elif d.startswith("claims"):
            kind = "claims"
        elif d.startswith("cited sources"):
            kind = "citations"
        elif d.startswith(("retrieved candidates", "freshness")):
            kind = "evidence"
        elif d.startswith("unanswered parts"):
            kind = "abstention"
        elif d.startswith("conflicts"):
            kind = "conflict"
        elif d.startswith("structured"):
            kind = "structured"
        elif d.startswith("tenant"):
            kind = "tenant"
        else:
            kind = "answer"
        if kind not in out:
            out.append(kind)
    return out


def percentile(values: list[float], q: float) -> float | None:
    """The q-th percentile (0 to 100, linear between neighbours) of a list of numbers; None for an empty list."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)
