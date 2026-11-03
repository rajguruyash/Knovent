# Evaluation of the language-model layer

## What this measures, and what it does not

The verification layer is the part of the language-model integration that can be measured **without a provider or a key**: given a stored answer and a rewrite of it, does `runtime/verify.py` accept honest rewordings and catch rewrites that change the facts?

`scripts/llm_evaluation.py` takes **every stored FAQ answer of the example knowledge bases** (Pawfect, a bike shop and six unrelated domains: 92 answers) and rewrites each in the ways that apply to it: honest (identical, a friendly opening, sentences reordered), harmless (a closing pleasantry that adds no fact), and tampered (a figure changed, a figure left out, an unsupported claim added, a guarantee added, a link added, a yes turned into a no, a condition left out, a unit changed, a planted instruction obeyed).

**It does not measure a language model.** The "model" is a script. How often a real provider tampers with facts, how natural its wording is, its latency and its cost per answer are **not measured** here: that needs a real key (`Blocked by external credentials`). What the numbers say is what the checker does with each kind of change.

## Result (no model of meaning)

| Rewrite | Applies to | Result |
|---|---|---|
| honest: identical | 92 | 92 of 92 accepted |
| honest: friendly opening | 92 | 92 of 92 accepted |
| honest: sentences reordered | 38 | 38 of 38 accepted |
| harmless: closing pleasantry | 92 | 0 of 92 accepted |
| tampered: figure changed | 60 | 0 of 60 **missed** |
| tampered: figure left out | 17 | 0 of 17 **missed** |
| tampered: unsupported claim added | 92 | 0 of 92 **missed** |
| tampered: guarantee added | 92 | 0 of 92 **missed** |
| tampered: link added | 92 | 0 of 92 **missed** |
| tampered: yes turned into no | 44 | 0 of 44 **missed** |
| tampered: condition left out | 4 | 0 of 4 **missed** |
| tampered: unit changed | 17 | 0 of 17 **missed** |
| tampered: planted instruction obeyed | 92 | 0 of 92 **missed** |

**Tampered rewrites missed: 0 of 510. Honest rewrites refused: 0 of 222.** The closing pleasantry is refused every time: a sentence like "Let me know if you need anything else" is a claim the evidence does not contain, and the checker has no way to know it is harmless. That is a strictness cost, accepted on purpose: the stored wording is used instead.

With the MiniLM model supplied as the measure of closeness in meaning (`--with-meaning`): tampered missed 0 of 510, honest refused 0 of 222.

## How much to trust these numbers

**Not much more than "the layer works on the cases it was built for".** The first run of this script, before any fix, missed **6 of 507** tampered rewrites (one figure written as a decade, three flipped yes/no answers, a dropped "only", a changed rate unit) and refused **7 of 222** honest ones (the **identical** text of three answers: a mixed-polarity sentence read as a contradiction, and a full stop eaten by the clock-time pattern, which merged two sentences and turned "closed on Sundays" into the whole text's weekday rule). All of those were fixed afterwards and the script was run again on the **same answers**, so the zero-and-zero above is tuned to this set and is optimistic. Cases of each fix are also in `tests/test_verify.py` (42 cases) so they stay fixed.

Other limits:

* The rewrites are mechanical. A real model's mistakes are subtler and more varied (it will, for example, paraphrase a condition into different words, or generalise "dogs" to "pets"), and the checker's blind spots (`runtime/verify.py: LIMITS`) are exactly such changes.
* The answers are short (one to four sentences). Long, multi-passage answers give more room for a combination that is true sentence by sentence and false together.
* 8 knowledge bases written by one author, in English (plus Spanish and Hinglish FAQs whose text the checker handles only through numbers, links and structure).

## Tested elsewhere

| Scenario | Where |
|---|---|
| LLM disabled: the stored answer, unchanged, with no call attempted | `tests/test_llm_answering.py` |
| Enabled, valid: reworded and checked; usage counted; no price means no cost | `test_llm_answering.py`, `test_hosted.py` |
| Missing key, invalid key, provider failure (401/403/404/429/5xx), timeout, redirect | `tests/test_llm.py`, `test_studio_llm.py`, `test_hosted.py` |
| Modified prices and numbers, dropped conditions, unsupported claims, added links, flipped policy, invalid references: rejected, fallback to the stored answer or the passages, fallback recorded | `tests/test_llm_answering.py` (parametrised) |
| A model that says INSUFFICIENT, or is asked with no evidence: nothing changes, nothing invented | `test_llm_answering.py` |
| Retrieval assistance cannot turn an unsupported question into a supported one | `test_llm_answering.py` |
| A document that instructs the model is quarantined; a model that obeys one is caught | `test_llm_answering.py`, `test_hosted.py` |
| The key is never in a response, an error, a log, an audit row, a stored message or an export | `test_llm.py`, `test_studio_llm.py`, `test_hosted.py` |

## Operational figures for the model's part

Per answer the result carries calls, tokens in and out, and seconds; hosted usage is stored. **Cost per generated response is not reported** unless a price per 1,000 tokens is configured, because prices change and none is built in. Failure rate and latency of a real provider are not measured.
