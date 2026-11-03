# Keeping the engine improving in production

The engine you downloaded can keep learning on its own, and it can rebuild itself, without anything calling the platform that made it.

```
production questions ──► teach (live) ──► retrain from source/ + what was learned ──► test against the live version ──► promote ──► roll back if needed
```

## 1. Teach it, live (no retraining, no downtime)

When a customer's question was answered wrongly, or not at all, tell the engine which entry it belongs to:

```bash
curl -s localhost:8080/feedback -H "Authorization: Bearer $FAQENGINE_FEEDBACK_KEY" \
  -d '{"question": "can I post it back", "entry_id": "returns", "verdict": "approve"}'
python ci/improve.py add returns "can I post it back"          # the same, from a terminal
```

The question is embedded with the model that is already there and becomes one more way of asking that entry, searched from the next request
on, and kept across restarts in `data/learned.jsonl`, which only grows: a retraining trains on every question in it, and the engine skips the ones a version already contains. Taught something by mistake? `python ci/improve.py forget <entry_id> "<question>"`. Only questions a person confirms are learned: if the engine learned from its own
unchecked answers it would teach itself its own mistakes. `verdict: "reject"` is only logged (`data/feedback.jsonl`).

- `/feedback` changes what the engine answers, so it needs a key whenever the server is reachable from the network (`FAQENGINE_FEEDBACK_KEY`, or `FAQENGINE_API_KEY`). With neither, it accepts feedback only when the server listens on this computer alone.
- `python runtime/serve.py --log-questions` keeps what customers asked in `data/asked.jsonl`, so you can find the questions to teach. It is off by default: they are your customers' words.
- Limits: 300 characters a question, 60 learned questions per entry, 5000 in all. Past that, retrain.

## 2. Rebuild it, from source, and test before it goes live

```bash
pip install <the faqengine tool>             # once; retraining needs it, running the engine does not
python ci/improve.py retrain
```

It retrains on `source/files/` (the files the engine was made from) plus every learned question, builds a candidate, and scores the candidate and the live
version on the same questions of the **deployment regression suite**, `source/regression.csv` (questions that are near-duplicates of anything it was trained on are removed first, so
learned questions cannot leak into it). The regression suite decides which version goes live, so it is not a test set; the held-out benchmark (`source/heldout.csv`) is only carried along for reporting and is never read here. Then:

| Outcome | When | What happens |
|---|---|---|
| `promoted` | the candidate is not worse on right-entry-first and not worse on MRR@10 | it becomes live; the previous version is saved in `versions/` (the newest 5 are kept) |
| `kept_current` | the candidate is worse | nothing changes, the exit code is 3, and the numbers are in `versions/reports/` |
| `staged` | there is no regression suite (or fewer than 20 questions to check against) | the candidate waits in `versions/staged-<time>/`: look at it, then `python ci/improve.py promote staged-<time>` |

A running `serve.py` loads the new version on its next request. `python ci/improve.py rollback` goes back to the previous one.

Retraining recalibrates the confidence lines automatically; set them again afterwards if you had set them by hand.

**Web pages stay fresh.** If the knowledge base includes web pages, `source/web.json` lists them. `python ci/improve.py retrain --refresh-web` reads them again first (a page that
cannot be read today keeps its old copy, with a note in the report), so a change on your site reaches the engine through the same test-before-promote gate.

## 3. Let a pipeline do it

`github-actions.yml` is a ready workflow: weekly, it pulls what production learned (`GET /learned`), runs `retrain`, and commits the new version only if it was promoted.
Your usual deployment then ships the commit. Fill in `FAQENGINE_INSTALL` at its top: the tool is not on a public package index, so say where to install it from.
Any other scheduler works the same way: cron, a CI job, or a button, running `python ci/improve.py retrain`.

## What this does not do

- It does not read your customers' minds: it learns only from questions a person confirmed, so how fast it improves is how much you teach it.
- The regression suite is the judge. If it is small or written by one person, a "not worse" promotion is weak evidence; add to `source/regression.csv` as you learn what customers really ask, and keep those questions out of your held-out benchmark.
