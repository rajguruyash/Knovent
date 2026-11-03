# Evidence architecture

**Similarity is not sufficiency.** "How much does a dog bath cost?" is very close to "What services do you offer?", whose answer lists a bath and no price. A cosine of 1.00 changes nothing about whether the stored answer contains the requested fact. Three engines keep the questions apart:

```
 question ──► RETRIEVAL ──► candidates + scores ──► EVIDENCE ──► assessment ──► RESPONSE ──► what is said
              (runtime/retrieval.py)               (runtime/evidence.py)       (runtime/response.py)
                                                                                    │ optional
                                                                       GENERATE ◄───┤ (runtime/generate.py)
                                                                       VERIFY   ────┘ (runtime/verify.py)
              runtime/answer.py is the only place that calls them in this order.
```

* **Retrieval** finds candidates. It does not judge them (`docs/retrieval-architecture.md`).
* **Evidence** decides what the candidates actually support. Standard library only; a sentence-similarity function is its only optional input. **It has no language model and works identically with the model switched off.**
* **Response** turns the assessment into words. It adds no fact.
* A language model, if one is configured, can only reword what evidence already established or suggest other searches; verification checks its text (`docs/llm-integration.md`).

## What evidence assesses

The question is split into **parts** ("Are you open on Mondays *and* do you accept UPI?" is two; "What are your hours and address?" is two). Each part is judged against each candidate text and gets one class:

| Class | Meaning | In the task's words |
|---|---|---|
| `direct` | the text states it in the question's own words, with the kind of detail asked for | directly supported |
| `semantic` | a sentence is close in meaning **and** a second, independent sign agrees (shared words, or the entry itself was retrieved as a close match), and the kind of detail asked for is there; and it must stand out from other entries' sentences | semantically supported |
| `related` | same subject, but the requested fact is not established | related but insufficient |
| `none` | nothing in the text bears on it | unsupported |

The whole question then gets an `evidence_class`: `direct`, `semantic`, `partial` (some parts supported), `related_insufficient`, `contradiction`, `no_evidence`. Contradictory sources and partial support are outcomes in their own right.

**Kinds of detail.** A question that asks *how much* (a price), *how long*, *what time*, *where*, *how to contact*, *how many*, or that mentions a figure, needs a figure **of that kind** in the answer: a price (₹, $, €, £, "rupees", "pounds"…), a duration ("45 minutes", "7 business days", "2 weeks", "3 months", "2 years"), a clock time or weekday, a place, a phone or address, a count. An answer with no such figure says nothing about it, however similar the question. Numbers that describe the asker's situation ("my 2 dogs") are not demanded of the answer.

**Conflicts.** Two supporting or closely related sources that give *different amounts of the same kind* (two prices, two durations, two clock times), or one saying yes where the other says no about the same thing, are reported as a conflict, with both named, each with its source and the sentence it says it in. Neither is chosen. Four rules keep unrelated sentences from contradicting each other (each was found by a false conflict, then tested):

1. A "related" source only counts if it shares a **specific subject word** with the question, not just the kind of figure: "delivery takes 1 to 2 days" does not contradict "a repair takes 2 to 3 days". Verbs, time words ("after", "within") and units ("day", "month") are not subject words: "the reset link expires after 1 hour" says nothing about what happens to data *after* you cancel.
2. A yes/no disagreement needs **both sentences to be about at least two of the question's own distinguishing words** (or the one it has). "Staff cannot administer medication" and "grooming may be paused if a dog shows distress" both mention staff or dogs; they do not disagree about medicine.
3. Sources that are **equally close** to the question are peers, not rivals: when a FAQ answer and a policy page both say when invoices are due, neither has to "stand out" from the other for the question to be answered, and if they differ it is a conflict. A source that is clearly *less* close than the best one is still a rival, so "Slack takes 5 minutes" is not set against "Salesforce takes 60".
4. Durations are compared in minutes whatever they are written in (minutes, hours, days, weeks, months, years).

**Conditions and exceptions.** The assembled answer quotes the supporting **sentence whole**, so a condition in it travels with it ("only if the item is unused and returned within 7 days"). A model's rewording is checked for them separately (`docs/llm-integration.md`).

## Several kinds of source at once

The same assessment runs whatever the candidates came from (a FAQ answer, a passage of a document, a spreadsheet row, a JSON record, a web page). Nothing in it knows or cares which: a FAQ answer is used verbatim only when it is the **strongest** support for every part, otherwise the sentence that supports a part is quoted from where it is, with its reference. A question that needs two sources ("what does the Pro plan cost and how long does the Slack integration take?") is two parts, each supported by the best candidate for it, each cited, with the sources that corroborate it listed (`corroborated_by`).

* **A pronoun in a later part** ("How long does the Slack integration take to set up, **and which plans have it**?") is read as the first named thing of the part before it ("which plans have Slack"). Only a name (a capitalised word or run of words that does not start the question; not a weekday or month) is carried over; with none the part is left alone.
* **"How much storage…"** asks for an amount, not a price. "How much" is a price only when it is followed by a verb or preposition of cost ("how much is / does / for / per…") or ends the question.
* **Names that are not in the knowledge.** A capitalised name in the question that appears nowhere in the knowledge ("Do you ship to Zorblax?") cannot be supported by a sentence that is merely close in meaning.
* **Outdated material.** Sources that expired, have not started or were replaced never reach the candidates (`docs/retrieval-architecture.md`). If the question would have been answered by such material and by nothing else, the response is `insufficient` with `outdated` (source, reason, section) and a message that says the only information found is out of date and was not used. The out-of-date facts are not given.

## What comes out (`answer.respond` / `Engine.respond`)

Plain data. Besides the answer or message:

* `outcome`: `faq` (a stored answer supports every part) · `grounded` (assembled from passages, at least one only from a document or page) · `partial` (what is supported, plus what is not) · `insufficient` · `conflict` · `clarification`
* `parts`, `unanswered_parts`, `part_assessments` (per part, per candidate: class, word coverage, meaning closeness, reason), `evidence_class`, `sources` (each with `ref`, `id`, `kind`, `source`, `location`, the supporting text), `conflicts` (the pair, the reason, both texts)
* **Separate signals**, never merged: `retrieval_similarity` (a cosine; "not a probability"), `answer_sufficiency` (the share of the question's parts ONE FAQ answer explicitly supports), `evidence_support` (the share supported by what is finally used), `response_confidence` (a category from rules, "not a calibrated probability")
* `escalate`, `limits`, and, from the pipeline: `retrieval` (candidates with their scores, diagnostics, anything quarantined), `llm`, `verification`, `fallback`, `assist`, `latency_ms`

**Clarification** is deliberately narrow: only when nothing answers the question *and* either it names nothing ("what about that?", "?") or it is a few words with no subject and two or more different entries fit about equally. Anything else that nothing answers is `insufficient`. A named, specific question that the knowledge base does not cover stays `insufficient` (checked on a dog-daycare question).

## Measured

`docs/evidence-results.md`: coverage, correct answer rate, false-answer rate, abstention precision and recall, sufficiency precision and recall, multi-part completion, partial handling and conflict detection, on development, regression and locked sets, with the caveat that the sets are small. This stretch changed the evidence engine in four ways, each found by a failing test and checked on development and regression (unchanged there):

1. **"7 business days", "2 weeks", "3 months"** are durations (they were not, so a refund-time question was reported unanswerable by the very sentence that answered it).
2. **"How long", "how soon", "how quickly"** no longer demand the literal word "long" in the answer: the duration check judges the detail.
3. **"45 pounds"** is a price.
4. **A related source must share a specific subject word** to count as a possible contradiction.

## Limits (these are in every response as `limits`)

Sufficiency is judged from words, the kind of detail asked for and, when a model is available, closeness in meaning. It can miss a paraphrase that shares no words and sits far from the question in meaning, and can be satisfied by a sentence that is close in meaning but says something else. Conflicts are found for numbers with units and for yes/no statements about the same thing, not for every kind of disagreement. Several FAQ entries, or an entry and a passage, can support the same fact; which is cited is the strongest one. The rules were written by one author against one domain's vocabulary (dog grooming, with a few others); a very different domain may need its own words in the kinds of detail.
