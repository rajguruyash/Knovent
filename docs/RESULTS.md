# Phase 0 results: does training on your own FAQ help?

**Yes, modestly, and only when each FAQ entry has more than one phrasing.** Measured on 30-entry knowledge
bases built from real customer questions (setup below), `all-MiniLM-L6-v2` as the base model, three random
knowledge bases per row, all numbers on questions the model never saw.

| Phrasings per entry | Untuned Hit@1 | Tuned Hit@1 | Gain | Wrong first answers removed |
|---|---|---|---|---|
| 1 | 72.2% | 72.1% | −0.1 pt | none |
| 2 | 77.8% | 80.4% | +2.6 pt | 12% |
| 4 | 82.2% | 86.0% | +3.8 pt | 21% |
| 8 | 87.8% | 91.5% | +3.7 pt | 30% |
| all (about 120: "we have query logs") | 95.4% | 95.8% | +0.4 pt | 8% (2 runs) |

- **From 2 phrasings on, tuning beat the untuned model in 9 of 9 runs**, clearly so on each run's own ~1,200 test
  questions in 8 of them (95% bootstrap interval above zero). It also beat the strongest off-the-shelf
  alternative, the untuned model combined with BM25 using a weight chosen on dev, by +2.8 points on average
  (range +0.2 to +4.7; clear in 8 of 9).
- **With one phrasing there is nothing to learn from.** The style variants (no punctuation, keywords, typos)
  do not add new wording. The safeguard kept the untuned weights in 2 of 3 runs, and in all 3 the untuned + BM25
  hybrid was 0.9 to 2.0 points better than the tuned model. Do not train on one phrasing.
- **With a lot of real data, the untuned model is already close to the ceiling** (95.4%), and tuning adds little.
  The benefit lives in the few-phrasings range, which is where a small business is.
- **BM25 alone is 11 to 18 points behind the untuned model.** Adding it to the untuned model helps by −0.1 to
  +1.4 points: a hybrid is cheap insurance, not a large gain.
- **"Answer directly and skip the LLM" was not established.** A direct-answer threshold certified at 95% precision
  (90% confidence bound) on 150 dev questions was found in only 6 of 24 model-runs. Where it was, it was right
  95.5% to 98.1% of the time on answerable questions, but 81% to 90% once out-of-scope questions (45% of this test
  traffic) were counted. The floor sent 41% to 61% of out-of-scope questions to "don't know" and kept 94% to 100% of
  them off the direct path. Telling answerable from out-of-scope questions by score alone gave an AUROC of
  0.85 to 0.92, 0.00 to 0.02 higher for the tuned model.

## Iteration 3: any field, any language (2026-10-31)

Full table: [domain-sweep.md](domain-sweep.md), produced by `python scripts/domain_sweep.py --multilingual` on `examples/domains/` (six small FAQs written for this, none of them from WASHO):
a clinic, software support, a restaurant, general trivia, a Spanish hotel and a Hinglish shop (Hindi in Latin letters). 12 entries each, 11 to 13 typed held-out test questions each,
dev and test with the same mix of question kinds. **The numbers are from very few questions (one question is 8 to 9 points) and one author wrote both the FAQs and the questions: read them as
"did anything fall apart", not as accuracy.**

- **English, four unrelated fields:** the model gets 85% to 100% of the test questions right first (keyword search 62% to 91%); nothing about the subject mattered. Unrelated questions are refused 100% of the time; near-topic ones (about the subject, not in the FAQ) are the weak spot (0% to 100% of 2 questions, 50% at best in general).
- **Trivia, whose subjects are the same as the generic off-topic questions:** 12 of the 30 generic questions are recognised as covered by the FAQ and left out of the refusal check (without that, a FAQ that answers "what is the capital of France" would be scored as failing to refuse it).
- **Spanish:** the English-only default model gets 55% of the questions right first and keyword search 82%; the multilingual model gets 91%. The report says up front that the text looks like Spanish and offers the multilingual model.
- **Hinglish:** neither model is good (55% English-only, 45% multilingual) and **keyword search is better (73%)**. Romanised Hindi has no standard spelling, which embedding models are not trained on. The report now says when keyword search beats the model; a hybrid (keywords and model together) is the obvious next step and is not built.
- **With the wrong model the refusal line is meaningless** (for Hinglish with the English model, out-of-scope questions score higher than answerable ones), which is why the language is checked and warned about before training, and why the refusal line is not left to a couple of dev questions (it borrows generic ones and is capped at the weakest tenth of right matches).
- **Odd data shapes** (one entry, two entries, no answers, duplicate questions, a 30,000-word answer, emoji, numbers only, one-word questions) all complete without error; the ones too small to judge training skip it and say so.

## Iteration 2: the WASHO FAQ, previous version against improved version (2026-10-31)

Full report: [washo-engine-report.md](washo-engine-report.md), produced by `faqengine compare` (3 seeds, same held-out questions for both versions,
near-duplicates of either version's phrasings removed, previous entries merged in the improved FAQ credited by information).

- Right entry first on 100 held-out test questions: previous engine **73% → improved 91%** (+18.0 points, 95% interval +8.0 to +28.0); MRR@10 0.831 → 0.949.
- Where the gain comes from: the better FAQ data alone (same unchanged base model) 73% → 85% (+12.0, +3.0 to +22.0); training on top 85% → 91% (+6.0, +0.0 to +12.0) for seed 0.
- Did training help? Across seeds the gain over the base model was +6.0, +10.0 and +10.0 points. The improvement is *demonstrated* (interval excludes zero) in 2 of 3 seeds; for seed 0 the interval touches zero, so it is not claimed. The trained model was the default in 3 of 3 runs.
- Refusal is the weak spot: 100% of unrelated out-of-scope questions are refused, but only 25% of near-topic ones (UPI, working hours, membership prices), in both versions. A single score line cannot separate "about washing, not in the FAQ" from "answerable"; those questions go to the LLM band instead of "not in the FAQ". Answerable questions wrongly refused: 7% → 2%.
- Caveat: the held-out questions and the alternate phrasings were written by the same author, not customers, so absolute figures are probably optimistic; the comparisons are fair because every system answers the same questions.

## What this means for the product

1. **Phrasing generation is part of training, not an extra.** The gain comes from phrasing diversity, so the
   studio must get to at least 2, better 4 or more, phrasings per entry (the customer's own LLM key, or real
   questions) before it trains, and must say plainly when it will not train because there is too little to learn from.
2. **Pitch it honestly.** Against an untuned small model plus BM25 this is about +3 points of Hit@1, or 12% to
   30% fewer wrong first answers, on a proxy. The lasting differentiators are ownership and portability, data
   that stays on the customer's machine, a report that shows the real lift (including when it is zero), routing
   by calibrated confidence, and the cost per answer. Not a dramatic accuracy jump.
3. **Show per business whether confident answers can skip the LLM.** It did not certify on this fine-grained
   dataset with 150 dev questions. It needs more dev questions, a lower target, or entries with more distinct
   topics, and has to be measured on the customer's real FAQ before it is promised.
4. **Keep the safeguards.** The dev check and the fall back to untuned weights are what turned "tuning did
   not help" into a harmless no-op instead of a worse model.
5. **Next measurement:** whether LLM-written paraphrases (cleaner and less varied than real customers' wording)
   give the same gain as the real phrasings used here. The code is built and tested against a fake server; the run
   needs an LLM key and is the last unknown before building the training screens (commands below).

## Tables

### Main runs (3 knowledge bases per row)

### hit@1 on unseen test questions (mean ± 95% interval over runs)

| Base | Entries | Answer text | Phrasings per entry | Runs | BM25 (keywords) | Untuned model | Tuned model | Untuned + BM25 (weight set on dev) | Tuned + BM25 (weight set on dev) | Tuned − untuned | Tuned beat untuned on dev |
|---|---|---|---|---|---|---|---|---|---|---|---|
| minilm-l6 | 30 | label | 1 | 3 | 0.583 ± 0.030 | 0.722 ± 0.040 | 0.721 ± 0.044 | 0.736 ± 0.038 | 0.734 ± 0.048 | -0.001 ± 0.005 | 1/3 |
| minilm-l6 | 30 | label | 2 | 3 | 0.602 ± 0.045 | 0.778 ± 0.020 | 0.804 ± 0.028 | 0.783 ± 0.031 | 0.806 ± 0.029 | 0.026 ± 0.028 | 3/3 |
| minilm-l6 | 30 | label | 4 | 3 | 0.646 ± 0.014 | 0.822 ± 0.009 | 0.860 ± 0.048 | 0.830 ± 0.024 | 0.860 ± 0.048 | 0.038 ± 0.050 | 3/3 |
| minilm-l6 | 30 | label | 8 | 3 | 0.714 ± 0.034 | 0.878 ± 0.038 | 0.915 ± 0.045 | 0.882 ± 0.030 | 0.914 ± 0.038 | 0.037 ± 0.028 | 3/3 |

### mrr@10 on unseen test questions (mean ± 95% interval over runs)

| Base | Entries | Answer text | Phrasings per entry | Runs | BM25 (keywords) | Untuned model | Tuned model | Untuned + BM25 (weight set on dev) | Tuned + BM25 (weight set on dev) | Tuned − untuned | Tuned beat untuned on dev |
|---|---|---|---|---|---|---|---|---|---|---|---|
| minilm-l6 | 30 | label | 1 | 3 | 0.704 ± 0.023 | 0.820 ± 0.003 | 0.820 ± 0.003 | 0.829 ± 0.006 | 0.828 ± 0.010 | -0.000 ± 0.002 | 1/3 |
| minilm-l6 | 30 | label | 2 | 3 | 0.723 ± 0.014 | 0.856 ± 0.015 | 0.873 ± 0.004 | 0.860 ± 0.021 | 0.874 ± 0.007 | 0.017 ± 0.018 | 3/3 |
| minilm-l6 | 30 | label | 4 | 3 | 0.756 ± 0.018 | 0.890 ± 0.012 | 0.915 ± 0.029 | 0.895 ± 0.020 | 0.915 ± 0.027 | 0.026 ± 0.025 | 3/3 |
| minilm-l6 | 30 | label | 8 | 3 | 0.810 ± 0.010 | 0.925 ± 0.022 | 0.949 ± 0.030 | 0.928 ± 0.016 | 0.949 ± 0.027 | 0.024 ± 0.018 | 3/3 |

### Answering with thresholds chosen on dev only

Direct answers must be right at least 95% of the time (90% confidence bound on dev); the floor keeps 95% of correctly answered dev questions. Out-of-scope questions are test questions about topics that are not in the knowledge base.

| Base | Phrasings | Model | Runs with a certified direct threshold | Direct precision in those runs (answerable questions) | Direct precision in those runs (out-of-scope counted wrong) | Answerable questions answered directly (all runs) | Out-of-scope kept off direct | Out-of-scope sent to "don't know" | AUROC answerable vs out-of-scope |
|---|---|---|---|---|---|---|---|---|---|
| minilm-l6 | 1 | Untuned | 1/3 | 0.981 | 0.899 | 0.085 ± 0.364 | 0.991 ± 0.040 | 0.413 ± 0.102 | 0.848 ± 0.032 |
| minilm-l6 | 1 | Tuned | 1/3 | 0.981 | 0.899 | 0.085 ± 0.364 | 0.991 ± 0.040 | 0.416 ± 0.108 | 0.848 ± 0.031 |
| minilm-l6 | 2 | Untuned | 2/3 | 0.955 ± 0.276 | 0.899 ± 0.009 | 0.179 ± 0.403 | 0.985 ± 0.042 | 0.493 ± 0.077 | 0.865 ± 0.029 |
| minilm-l6 | 2 | Tuned | 1/3 | 0.967 | 0.867 | 0.154 ± 0.663 | 0.978 ± 0.095 | 0.509 ± 0.186 | 0.879 ± 0.022 |
| minilm-l6 | 4 | Untuned | 0/3 | n/a | n/a | 0.000 ± 0.000 | 1.000 ± 0.000 | 0.499 ± 0.141 | 0.881 ± 0.043 |
| minilm-l6 | 4 | Tuned | 0/3 | n/a | n/a | 0.000 ± 0.000 | 1.000 ± 0.000 | 0.511 ± 0.051 | 0.899 ± 0.040 |
| minilm-l6 | 8 | Untuned | 0/3 | n/a | n/a | 0.000 ± 0.000 | 1.000 ± 0.000 | 0.606 ± 0.149 | 0.906 ± 0.026 |
| minilm-l6 | 8 | Tuned | 1/3 | 0.955 | 0.807 | 0.258 ± 1.109 | 0.941 ± 0.254 | 0.604 ± 0.281 | 0.923 ± 0.025 |

### Ceiling: every real train question for the 30 entries (2 knowledge bases, 150 steps, no style variants)

| Base | Entries | Answer text | Phrasings per entry | Runs | BM25 (keywords) | Untuned model | Tuned model | Untuned + BM25 (weight set on dev) | Tuned + BM25 (weight set on dev) | Tuned − untuned | Tuned beat untuned on dev |
|---|---|---|---|---|---|---|---|---|---|---|---|
| minilm-l6 | 30 | label | all | 2 | 0.847 ± 0.002 | 0.954 ± 0.000 | 0.958 ± 0.005 | 0.953 ± 0.011 | 0.957 ± 0.000 | 0.004 ± 0.005 | 2/2 |

### Check: no answer text indexed (2 knowledge bases per row)

| Base | Entries | Answer text | Phrasings per entry | Runs | BM25 (keywords) | Untuned model | Tuned model | Untuned + BM25 (weight set on dev) | Tuned + BM25 (weight set on dev) | Tuned − untuned | Tuned beat untuned on dev |
|---|---|---|---|---|---|---|---|---|---|---|---|
| minilm-l6 | 30 | none | 2 | 2 | 0.540 ± 0.472 | 0.745 ± 0.331 | 0.773 ± 0.332 | 0.746 ± 0.352 | 0.773 ± 0.332 | 0.028 ± 0.000 | 2/2 |
| minilm-l6 | 30 | none | 8 | 2 | 0.709 ± 0.129 | 0.870 ± 0.176 | 0.914 ± 0.133 | 0.874 ± 0.129 | 0.914 ± 0.133 | 0.043 ± 0.043 | 2/2 |

Same direction and size as with an answer sentence (+2.8 points at 2 phrasings, +4.4 at 8), so the result does not depend on the answer text given to each entry.

### Each run

| Phrasings | Seed | Untuned Hit@1 | Tuned Hit@1 | Gain [95% interval on ~1.2k test questions] | Tuned − (untuned + BM25) | Steps | Training seconds | Beat untuned on dev |
|---|---|---|---|---|---|---|---|---|
| 1 | 0 | 0.738 | 0.738 | +0.000 [+0.000, +0.000] | -0.009 [-0.022, +0.003] | 24 | 15 | no |
| 1 | 1 | 0.706 | 0.703 | -0.003 [-0.012, +0.004] | -0.016 [-0.026, -0.005] | 32 | 46 | yes |
| 1 | 2 | 0.722 | 0.722 | +0.000 [+0.000, +0.000] | -0.020 [-0.037, -0.005] | 24 | 36 | no |
| 2 | 0 | 0.785 | 0.816 | +0.031 [+0.014, +0.047] | +0.026 [+0.010, +0.041] | 56 | 67 | yes |
| 2 | 1 | 0.780 | 0.793 | +0.013 [-0.002, +0.028] | +0.002 [-0.014, +0.018] | 40 | 58 | yes |
| 2 | 2 | 0.769 | 0.803 | +0.034 [+0.021, +0.049] | +0.034 [+0.021, +0.049] | 40 | 88 | yes |
| 4 | 0 | 0.821 | 0.841 | +0.020 [+0.004, +0.038] | +0.021 [+0.003, +0.039] | 96 | 126 | yes |
| 4 | 1 | 0.827 | 0.861 | +0.034 [+0.015, +0.053] | +0.030 [+0.013, +0.048] | 80 | 114 | yes |
| 4 | 2 | 0.820 | 0.879 | +0.059 [+0.039, +0.080] | +0.040 [+0.022, +0.058] | 96 | 181 | yes |
| 8 | 0 | 0.861 | 0.900 | +0.039 [+0.024, +0.056] | +0.032 [+0.018, +0.047] | 160 | 200 | yes |
| 8 | 1 | 0.888 | 0.935 | +0.047 [+0.030, +0.063] | +0.047 [+0.030, +0.063] | 160 | 220 | yes |
| 8 | 2 | 0.885 | 0.910 | +0.024 [+0.012, +0.037] | +0.020 [+0.007, +0.034] | 128 | 208 | yes |

## How this was measured

**Task.** Banking77: real customer-support questions, 77 intents (CC BY 4.0). Each run picks 30 random intents
and turns each into one FAQ entry. The entry's questions are `k` real customer phrasings from the train split, the
stand-in for the phrasings a business writes, or an LLM generates, per entry. The answer text is the intent
name in a sentence ("This answers questions about card arrival."), so the combined view has some answer
words to match, like a real FAQ would.

**What the model sees in training:** only those `k` phrasings per entry plus style variants made from them
(no punctuation, keywords only, dropped words, typos). Nothing else from the train split.

**Dev questions** (5 per entry, from the rest of the train split) stop training early, choose the BM25 weight in
the hybrid baselines, and set the answer thresholds. **Test questions** come from the official test split, so
they are never seen in training or tuning: about 1,200 answerable ones, plus 1,000 about the 47 intents that are
not in the knowledge base, which stand in for out-of-scope traffic.

**Systems compared on the same test questions:** BM25 (keywords); the untuned `all-MiniLM-L6-v2`; the same
model tuned on the FAQ; and each model combined with BM25 using a weight chosen on dev, so the hybrid
baseline is as good as dev can make it. Every system searches the same views.

**Safeguard under test.** The tuned model is the best checkpoint on dev, or the untuned weights if no
checkpoint beats them. So "tuned" can never be worse than "untuned" on dev, only on unseen questions, which is
what the test set measures.

**Learning rate** was chosen on dev, on one scenario (seed 10, 4 phrasings) that no reported run uses: 2e-5, 5e-5 and
1e-4 gave dev MRR gains of +0.029, +0.021 and +0.030. They are within noise of each other; 1e-4 was taken because it
was nominally best and needed 40% fewer steps (96 against 160), which matters on a small laptop. Test results of that
sweep are in `runs/sweep.jsonl` and were not used to choose.

## What this does not tell us

- **A proxy, not your customers.** Banking77 questions are short intent-style utterances and the answers are
  one line. Real FAQs have long answers and customers who phrase things differently from any dataset. Treat
  these numbers as the shape of the effect, and re-run `faqengine evaluate` on the real FAQ and some real
  customer questions before believing a lift.
- **`k` phrasings are real customer questions, which LLM paraphrases are not.** LLM-written paraphrases
  are cleaner and less varied than what customers type, so the `k` rows are probably somewhat optimistic for
  LLM-generated phrasings. The pipeline does not generate paraphrases yet.
- **Few runs.** Each row is three random 30-intent knowledge bases (two for the ceiling), so the intervals across
  runs are wide and the "tuned − untuned" interval at 2 phrasings touches zero. The consistency across runs
  (9 of 9 better, 8 of 9 clear on their own test questions) is the stronger evidence than any single row.
- **Hard dataset.** Banking77 intents are fine-grained and often confusable (several are about card payments),
  which makes high-precision direct answers hard. A FAQ with distinct topics may certify far more easily.
- **One base model, one dataset.** Other base models (`bge-small`, `e5-small`) and other domains were not run.
- **A throttled laptop.** Everything ran on a fanless 8 GB MacBook (Apple M2) that sustained a fraction of its
  peak GPU speed, which is why the grid is small. See the next section.

## Reproduce

```bash
faqengine bench --base minilm-l6 --entries 30 --k 1,2,4,8 --seeds 0-2 --out runs/bench/main.jsonl
faqengine bench --base minilm-l6 --entries 30 --k all --seeds 0-1 --out runs/bench/ceiling.jsonl
faqengine bench --base minilm-l6 --entries 30 --k 2,8 --seeds 0-1 --answer none --out runs/bench/answer-none.jsonl
faqengine bench-summary runs/bench/main.jsonl
python scripts/sweep_hparams.py          # the learning-rate choice, on dev only
```

LLM-written phrasings against real ones, same intents and same first phrasing per seed (about 90 LLM calls, then
about 15 minutes of training; the key stays in your terminal):

```bash
export GEMINI_API_KEY=...
faqengine paraphrase --check
faqengine bench-paraphrases --seeds 0-2 --n 7             # writes data/paraphrases.json; resumable
faqengine bench --k 1 --seeds 0-2 --paraphrases data/paraphrases.json --out runs/bench/llm7.jsonl
faqengine bench-summary runs/bench/main.jsonl runs/bench/llm7.jsonl   # adds the paired comparison table
```

## Training on a small laptop: what we saw

Hardware: a fanless MacBook (Apple M2, 8 GB RAM, about 6 GB of disk free), other applications open.

- **Training is feasible.** A run is 24 to 160 steps (batch of about 30 questions and 60 documents, 22M
  parameters) and took 15 to 230 seconds. A 30-entry FAQ trains in one to four minutes.
- **But speed is not predictable.** Seconds per step: median 1.4, range 0.6 to 2.2. The very first run, on a cool
  machine, took 0.28. A 5-second matrix benchmark measured 2.8 TFLOPS on the GPU; a sustained 45-second one fell to
  0.5 TFLOPS after about 10 seconds. The same training run was roughly five times slower an hour later. An ETA has
  to come from observed steps and be re-estimated continuously, and a hardware probe has to measure sustained
  throughput, not a burst.
- **Memory is the other limit.** Apple's GPU backend held about 3.3 GB while the tensors in use were about 450 MB,
  on a machine with 8 GB and 2.5 GB of swap already in use. Padding sequences to a few fixed lengths did not change
  this. On a small laptop a "free memory" and "other apps are using the GPU" warning belongs on the training screen.
- **The exported engine is small and fast.** For the 10-entry example the package is 23 MB (17 MB zipped) with
  an int8 model whose output agrees with the PyTorch model at a cosine of 0.993 on average, and answers one question
  in about 2 ms (median, CPU, machine idle; about 9 ms while it was busy). It runs with numpy, onnxruntime and tokenizers only.
