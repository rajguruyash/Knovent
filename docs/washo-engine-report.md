> **Protocol note (added later).** This report was produced before the evaluation protocol was tightened: its "dev" questions came from the held-out file and chose the model and the
> refusal line, and the trained model was vetoed if it was worse on the test questions. Since then every row of a held-out file is a test question only, validation comes from questions
> generated from the FAQ, and test results never decide anything (they only decide what may be claimed). The figures below are therefore from the earlier protocol and have not been regenerated;
> `faqengine compare` runs the current one.

# Engine report: previous version vs improved version

Generated 2026-10-31 19:24:48. Everything below is measured on the same 100 held-out test questions (and 48 dev questions used only to choose), which were written separately from the training phrasings and checked for near-duplicates of them. Base model: `minilm-l6`.

## Summary

- **Right entry first (Hit@1):** previous engine 73% → improved engine 91%, +18.0 pts (95% interval +8.0 to +28.0).
- **MRR@10:** 0.831 → 0.949, +0.118 (95% interval +0.062 to +0.178).
- **Where the gain comes from:** the better FAQ data alone (same unchanged base model on both FAQs) moves Hit@1 73% → 85% (+12.0 pts (95% interval +3.0 to +22.0)); training on top of it moves 85% → 91% (+6.0 pts (95% interval +0.0 to +12.0)).
- **Did training help?** Not demonstrated (seed 0: +6.0 pts (95% interval +0.0 to +12.0)). Across 3 seeds the trained model's Hit@1 gain over the base model was +6.0, +10.0, +10.0 points; the default would be the trained model in 3 of 3 runs. Default for seed 0: **tuned**.
- **Refusal (validated separately):** previous engine refused 25% near-topic and 100% unrelated out-of-scope questions (wrongly refused 7% of answerable ones); improved engine 25% and 100% (wrongly refused 2%).
- **FAQ:** 30 entries with 30 phrasings → 21 entries with 220 phrasings (9 to 10 alternate phrasings each). No prices, policies or facts were added; every change is listed below.

## Overall, test questions (side by side)

| System | Hit@1 | P@3 | R@3 | P@5 | R@5 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|---|---|
| Previous: keyword search | 61% | 33% | 61% | 24% | 70% | 0.687 | 0.684 |
| Previous: base model | 73% | 43% | 80% | 31% | 86% | 0.831 | 0.831 |
| **Previous: engine as shipped** (= base model) | 73% | 43% | 80% | 31% | 86% | 0.831 | 0.831 |
| Improved: keyword search | 69% | 32% | 83% | 22% | 92% | 0.787 | 0.827 |
| Improved: base model (unchanged) | 85% | 37% | 94% | 24% | 97% | 0.908 | 0.929 |
| Improved: trained model (seed 0) | 91% | 37% | 96% | 24% | 99% | 0.949 | 0.960 |
| **Improved: engine as shipped** (= tuned model) | 91% | 37% | 96% | 24% | 99% | 0.949 | 0.960 |

## By type of question, test (previous → improved)

| Type | n | System | Previous Hit@1 | Improved Hit@1 | Previous MRR@10 | Improved MRR@10 | Previous nDCG@10 | Improved nDCG@10 |
|---|---|---|---|---|---|---|---|---|
| Paraphrases (conversational wording, synonyms) | 31 | Keyword search | 55% | 74% | 0.649 | 0.820 | 0.638 | 0.856 |
|  |  | Base model | 65% | 84% | 0.777 | 0.893 | 0.796 | 0.919 |
|  |  | Trained / shipped engine | 65% | 94% | 0.777 | 0.968 | 0.796 | 0.976 |
| Answer-word questions (the answer's own terms) | 15 | Keyword search | 87% | 73% | 0.889 | 0.856 | 0.861 | 0.893 |
|  |  | Base model | 73% | 80% | 0.833 | 0.865 | 0.856 | 0.898 |
|  |  | Trained / shipped engine | 73% | 87% | 0.833 | 0.917 | 0.856 | 0.937 |
| Typos and misspellings | 15 | Keyword search | 53% | 33% | 0.602 | 0.547 | 0.622 | 0.627 |
|  |  | Base model | 87% | 73% | 0.922 | 0.856 | 0.901 | 0.893 |
|  |  | Trained / shipped engine | 87% | 87% | 0.922 | 0.922 | 0.901 | 0.942 |
| Short or incomplete questions, abbreviations | 30 | Keyword search | 60% | 83% | 0.669 | 0.865 | 0.683 | 0.882 |
|  |  | Base model | 67% | 93% | 0.789 | 0.961 | 0.811 | 0.971 |
|  |  | Trained / shipped engine | 67% | 93% | 0.789 | 0.961 | 0.811 | 0.971 |
| Ambiguous questions (more than one entry is acceptable) | 9 | Keyword search | 56% | 56% | 0.681 | 0.704 | 0.650 | 0.772 |
|  |  | Base model | 100% | 89% | 1.000 | 0.944 | 0.860 | 0.938 |
|  |  | Trained / shipped engine | 100% | 89% | 1.000 | 0.944 | 0.860 | 0.935 |

Notes: in the "previous" columns the third system is the previous engine as shipped; in the "improved" columns it is the trained model from seed 0 (the row labelled shipped above is the base model when training did not win). Ambiguous questions accept several entries; R@K there is the share of acceptable entries found.

## Did training help? (improved FAQ, base model vs trained model, by seed)

| Seed | Dev Hit@1 base → trained | Test Hit@1 base → trained | Difference (95% interval) | Test MRR@10 base → trained | Default | Improvement demonstrated | Steps |
|---|---|---|---|---|---|---|---|
| 0 | 88% → 94% | 85.0% → 91.0% | +6.0 pts (+0.0 to +12.0) | 0.908 → 0.949 | tuned | no | 88 |
| 1 | 88% → 98% | 85.0% → 95.0% | +10.0 pts (+4.0 to +17.0) | 0.908 → 0.973 | tuned | yes | 308 |
| 2 | 88% → 96% | 85.0% → 95.0% | +10.0 pts (+3.0 to +18.0) | 0.908 → 0.970 | tuned | yes | 176 |

Rule for the default: the trained model is used only if it beat the base model on the dev questions AND is not worse on the test questions; otherwise the unchanged base model stays. An improvement is only claimed when the paired 95% interval on right-entry-first (Hit@1) excludes zero; a gain in ranking alone (MRR@10) is reported but not claimed.

## Refusal behaviour (validated separately)

Near-topic = about washing but not in the FAQ (for example UPI, working hours, membership prices); unrelated = anything else. The refusal line of the improved engine is chosen on dev questions; the previous engine is shown at the line it actually shipped with, and with a line re-chosen on dev the same way.

| Engine | Refusal line | Near-topic refused | Unrelated refused | Answerable wrongly refused | AUROC near-topic | AUROC unrelated |
|---|---|---|---|---|---|---|
| Previous engine, line as shipped | 0.27 | 25% (12) | 100% (12) | 7% (100) | 0.67 | 0.99 |
| Previous engine, line re-chosen on dev | 0.38 | 42% (12) | 100% (12) | 21% (100) | 0.67 | 0.99 |
| Improved base model | 0.51 | 33% (12) | 100% (12) | 9% (100) | 0.82 | 1.00 |
| Improved trained model | 0.47 | 25% (12) | 100% (12) | 2% (100) | 0.92 | 1.00 |

Threshold sweep, improved default (Your engine), test questions:

| Line | Near-topic refused | Unrelated refused | Answerable wrongly refused |
|---|---|---|---|
| 0.15 | 0% | 0% | 0% |
| 0.20 | 0% | 17% | 0% |
| 0.25 | 0% | 33% | 0% |
| 0.30 | 0% | 58% | 0% |
| 0.35 | 0% | 75% | 0% |
| 0.40 | 17% | 92% | 0% |
| 0.45 | 25% | 92% | 1% |
| 0.50 | 33% | 100% | 3% |
| 0.55 | 67% | 100% | 10% |
| 0.60 | 83% | 100% | 15% |
| 0.65 | 92% | 100% | 25% |
| 0.70 | 100% | 100% | 37% |

Answering without an LLM stays **off** in both versions: it needs proof from a person's own approved tests, which the Test page collects. Both thresholds are configurable (`--floor`, `--direct`, or the Test page).

## Confusable entries, before and after

Similarity of entry centres under the same unchanged base model on both FAQs, so only the FAQ differs. These are the closest pairs the previous engine's own report listed.

| Previous pair | Reported | Now (base model) | After |
|---|---|---|---|
| How much does a bike wash cost? / How much does it cost to wash a bike and a car? | 0.92 | 0.92 | `bike-wash` vs `bike-and-car-price`: 0.87 |
| How often is vacuum cleaning included in the Basic plan? / How often is vacuum cleaning included in the Pro plan? | 0.91 | 0.91 | `basic-vacuum` vs `pro-vacuum`: 0.87 |
| What does car deep cleaning cost compared with a body wash? / What is the difference between a car body wash and deep cleaning? | 0.86 | 0.86 | merged into one entry (`body-vs-deep`) |
| How much does car deep cleaning cost? / What is the price for SUV deep cleaning? | 0.84 | 0.84 | `car-deep-cleaning-price` vs `suv-deep-cleaning`: 0.74 |
| What services does WASHO offer? / Where does WASHO operate? | 0.81 | 0.81 | `services` vs `doorstep-area`: 0.65 |
| How much does car deep cleaning cost? / What does car deep cleaning cost compared with a body wash? | 0.81 | 0.81 | `car-deep-cleaning-price` vs `body-vs-deep`: 0.68 |

Closest pairs in the improved FAQ (base model): `basic-vacuum` ~ `pro-vacuum` 0.87; `bike-wash` ~ `bike-and-car-price` 0.87; `car-wash` ~ `car-wash-process` 0.82; `bike-and-car-price` ~ `car-body-wash-price` 0.79.

## Data and leakage checks

- Held-out: 184 questions kept (60 dev, 124 test), 1 removed as near-duplicates of a training phrasing of either version. 3 were typed by the owner in the app earlier.
- Test questions by type: paraphrases 31, answer-word questions 15, typos and misspellings 15, short or incomplete questions, abbreviations 30, ambiguous questions 9, out of scope, near the topic 12, out of scope, unrelated 12.
- No test question is a near-duplicate of any training phrasing (same words, typo or light rewording); the dev questions that chose the model are separate from the test questions that report it.
- Training phrasings were never split between train and test: all alternate phrasings are training data, and the held-out questions come from a separate file.
- The held-out questions and the alternate phrasings were written by the same author (not customers), so absolute figures are probably higher than real customers would get; the comparisons between systems are fair because every system answers the same questions.
- Answer-word questions reuse terms from the answers by design (they test whether answer wording leads to the entry).

## Entry-by-entry changes to the FAQ

| New entry | Change | Previous entries | New question |
|---|---|---|---|
| `services` | question and answer reworded (same facts) | faq-001: What services does WASHO offer? | What services does WASHO offer? |
| `bike-wash` | merged | faq-002: Do you wash bikes?; faq-003: How much does a bike wash cost? | Do you wash bikes, and how much does a bike wash cost? |
| `bike-and-car-price` | question reworded | faq-025: How much does it cost to wash a bike and a car? | How much does it cost to wash a bike and a car together? |
| `car-wash` | unchanged (phrasings added) | faq-004: Do you wash cars? | Do you wash cars? |
| `car-body-wash-price` | unchanged (phrasings added) | faq-005: What is the price of a car body wash? | What is the price of a car body wash? |
| `car-deep-cleaning-price` | question and answer reworded (same facts) | faq-006: How much does car deep cleaning cost? | How much does car deep cleaning cost? |
| `suv-deep-cleaning` | merged | faq-007: Do you clean SUVs?; faq-008: What is the price for SUV deep cleaning?; faq-026: Does WASHO offer SUV services? | Do you clean SUVs, and what does SUV deep cleaning cost? |
| `body-vs-deep` | merged | faq-023: What does car deep cleaning cost compared with a body wash?; faq-024: What is the difference between a car body wash and deep cleaning? | What is the difference between a car body wash and deep cleaning, and how do their prices compare? |
| `doorstep-area` | merged | faq-009: Do you provide doorstep washing?; faq-010: Where does WASHO operate?; faq-011: Do I need to take my vehicle to a washing centre?; faq-027: Can I get my car washed at my apartment?; faq-028: How do I find out whether my address is covered? | Do you provide doorstep washing, where do you operate, and how do I check my address is covered? |
| `car-wash-process` | unchanged (phrasings added) | faq-012: How is a car body wash performed? | How is a car body wash performed? |
| `foam` | unchanged (phrasings added) | faq-013: Do you use foam while washing cars? | Do you use foam while washing cars? |
| `drying` | unchanged (phrasings added) | faq-014: Do you dry the car after washing? | Do you dry the car after washing? |
| `interior-and-plans` | question reworded | faq-015: Is interior cleaning included? | Is interior cleaning included, and how do the Basic and Pro plans differ? |
| `basic-vacuum` | question and answer reworded (same facts) | faq-016: How often is vacuum cleaning included in the Basic plan? | How often is interior vacuum cleaning included in the Basic plan? |
| `pro-vacuum` | question and answer reworded (same facts) | faq-017: How often is vacuum cleaning included in the Pro plan? | How often is interior vacuum cleaning included in the Pro plan? |
| `custom-membership` | merged | faq-018: Can I get a customized membership?; faq-029: Can I ask about a custom price for multiple washes? | Can I get a customized membership or a custom price for multiple washes? |
| `weekly-frequency` | unchanged (phrasings added) | faq-019: Can I choose how many washes I need per week? | Can I choose how many washes I need per week? |
| `combine-washes` | unchanged (phrasings added) | faq-020: Can I combine different types of washes in a membership? | Can I combine different types of washes in a membership? |
| `become-member` | unchanged (phrasings added) | faq-021: How can I become a WASHO member? | How can I become a WASHO member? |
| `one-time-wash` | question and answer reworded (same facts) | faq-022: Can I ask for a one-time wash instead of a membership? | Can I ask for a one-time wash instead of a membership? |
| `contact` | unchanged (phrasings added) | faq-030: How can I contact WASHO? | How can I contact WASHO? |

## Assumptions made, and questions for the owner

Nothing below was invented: it is where the FAQ is silent, and what the engine does about it.

- **Basic vs Pro.** The only difference the FAQ states is how often interior vacuum cleaning is included (Basic once a month, Pro four times a month).
  If the plans also differ in price, number of washes or anything else, add it to `interior-and-plans`, `basic-vacuum` and `pro-vacuum`.
- **Membership prices** are not in the FAQ, so questions such as "what is the price of the Basic plan" or "membership cost per month" are treated as out of scope and refused.
- **What counts as a "bike".** The FAQ only says "bike". Words such as two-wheeler, scooter, motorcycle or bicycle were deliberately not added to any phrasing. Confirm what is covered.
- **SUVs.** Only SUV deep cleaning (₹250) is listed; an SUV body wash is not mentioned, so none is claimed.
- **Bike and car together.** The FAQ gives the two prices but no bundle price or discount; the entry says only that. If a bundle price exists, add it.
- **Not in the FAQ, so refused:** offers and discounts, payment methods, working hours, days of the week, booking and slots, cancellation and refunds, how long a wash takes, water and chemicals, other vehicle types, other cleaning services (polish, coating, engine bay).
- **Other localities.** The FAQ names Kharadi, Pune. A question about another area is answered with the service area and the instruction to contact WASHO to confirm.

### Merges (only where the previous answers carried the same information)

- `bike-wash` = previous "Do you wash bikes?" + "How much does a bike wash cost?" (both: bike washing, ₹65).
- `suv-deep-cleaning` = "Do you clean SUVs?" + "What is the price for SUV deep cleaning?" + "Does WASHO offer SUV services?" (all: SUV deep cleaning, ₹250).
- `body-vs-deep` = "What does car deep cleaning cost compared with a body wash?" + "What is the difference between a car body wash and deep cleaning?" (the same prices and the same distinction).
- `doorstep-area` = "Do you provide doorstep washing?" + "Where does WASHO operate?" + "Do I need to take my vehicle to a washing centre?" + "Can I get my car washed at my apartment?" + "How do I find out whether my address is covered?" (doorstep service in Kharadi, Pune, and contact WASHO to confirm an address).
- `custom-membership` = "Can I get a customized membership?" + "Can I ask about a custom price for multiple washes?" (customized plans; contact the team).

### Kept apart on purpose (they differ), and made easier to tell apart

- Bike only (`bike-wash`) vs bike and car together (`bike-and-car-price`).
- Basic (`basic-vacuum`) vs Pro (`pro-vacuum`), with an overview of both in `interior-and-plans`; each answer now also states the other plan's figure.
- Car body wash price (`car-body-wash-price`) vs car deep cleaning price (`car-deep-cleaning-price`) vs the comparison (`body-vs-deep`).
- Car deep cleaning (`car-deep-cleaning-price`) vs SUV deep cleaning (`suv-deep-cleaning`): each answer states both prices.
- Services overview (`services`, now listing the four services and memberships) vs where WASHO operates (`doorstep-area`).

## Reproduce

```
faqengine compare --previous examples/washo/faq.previous.csv --improved examples/washo/faq.improved.csv \
    --heldout examples/washo/heldout.csv --map examples/washo/previous_map.csv --notes examples/washo/NOTES.md --seeds 0,1,2 --out runs/washo-comparison
```
