# Domain sweep: the same pipeline on FAQs from unrelated fields and languages

Each folder in `examples/domains/` is a small FAQ with typed held-out questions written in other words (see `scripts/build_domain_examples.py`). Held-out columns are measured on that domain's test questions only; the dev questions chose the model and the refusal line. The FAQs and questions were written by one author, not customers, so absolute figures are optimistic: compare the columns, and look for anything that fell apart.

| Domain | Text detected as | Model | Entries | Test Qs | Keyword | Base model | Shipped (right first) | Trained? | Near-topic refused | Unrelated refused | Answerable wrongly refused | Generated check | Probes left out | Time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| clinic | English | minilm-l6 | 12 | 13 | 62% | 85% | **85%** | yes, base kept | 100% | 100% | 15% | 92% (1 probes left out) | 1 | 30 s |
| hinglish-store | Hinglish (Hindi in Latin letters) (warned) | minilm-l6 | 12 | 11 | 73% | 55% | **55%** | yes, kept | 0% | 0% | 0% | 92% (0 probes left out) | 0 | 51 s |
| hinglish-store | Hinglish (Hindi in Latin letters) | multilingual-e5-small | 12 | 11 | 73% | 45% | **45%** | yes, base kept | 0% | 100% | 0% | 97% (0 probes left out) | 0 | 141 s |
| restaurant | English | minilm-l6 | 12 | 11 | 73% | 100% | **100%** | yes, base kept | 50% | 100% | 27% | 94% (2 probes left out) | 2 | 38 s |
| saas | English | minilm-l6 | 12 | 11 | 64% | 91% | **91%** | yes, base kept | 0% | 100% | 0% | 86% (2 probes left out) | 2 | 25 s |
| spanish-hotel | Spanish (warned) | minilm-l6 | 12 | 11 | 82% | 55% | **55%** | yes, base kept | 0% | 0% | 0% | 96% (0 probes left out) | 0 | 55 s |
| spanish-hotel | Spanish | multilingual-e5-small | 12 | 11 | 82% | 91% | **91%** | yes, kept | 50% | 100% | 9% | 96% (0 probes left out) | 0 | 181 s |
| trivia | English | minilm-l6 | 12 | 11 | 91% | 100% | **100%** | yes, base kept | 0% | 100% | 0% | 96% (12 probes left out) | 12 | 21 s |
