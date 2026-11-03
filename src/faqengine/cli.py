"""Command line: faqengine check | train | evaluate | export | bench | bench-summary."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, fields
from pathlib import Path

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _seeds(text: str) -> list[int]:
    if "-" in text:
        lo, hi = text.split("-")
        return list(range(int(lo), int(hi) + 1))
    return [int(x) for x in text.split(",")]


def _ks(text: str) -> list[int | None]:
    return [None if x == "all" else int(x) for x in text.split(",")]


def _kb(args):
    from .kb import load_kb

    kb = load_kb(args.kb)
    for w in kb.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return kb


def cmd_check(args) -> int:
    kb = _kb(args)
    print(f"{len(kb)} entries, {len(kb.views)} searchable views, {sum(len(e.questions) for e in kb.entries)} question phrasings")
    from .language import detect_kb

    found = detect_kb(kb)
    print(f"language: {found['language']} ({found['evidence']})" + ("" if found["english"] else "; the default model is English-only, so try --base multilingual-e5-small"))
    return 0


def cmd_train(args) -> int:
    from .kb import load_queries
    from .train import TrainConfig, train

    kb = _kb(args)
    dev = load_queries(args.dev, kb) if args.dev else None
    if dev is None:
        print("warning: no --dev questions: training a fixed number of steps, with no early stopping and no check against the untuned model", file=sys.stderr)
    cfg = TrainConfig(**{f.name: getattr(args, f.name) for f in fields(TrainConfig) if getattr(args, f.name, None) is not None})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = (out / "events.jsonl").open("w")

    def on_event(e: dict) -> None:
        log.write(json.dumps(e) + "\n")
        if e["event"] == "eval":
            d = e["dev"]
            print(f"step {e['step']:>4}  train loss {e['train_loss']:.3f}  dev loss {d['loss']:.3f}  dev Hit@1 {d['hit@1']:.3f}  dev MRR@10 {d['mrr@10']:.3f}", flush=True)
        elif e["event"] == "stage" and e["stage"] in ("starting", "early_stop", "done"):
            print({k: v for k, v in e.items() if k != "event"}, flush=True)

    _, tlog = train(kb, dev, cfg, out, on_event)
    log.close()
    print(f"trained {tlog['steps']} steps in {tlog['train_seconds']:.0f} s on {tlog['device']}; saved to {out}")
    if dev is not None:
        b, a = tlog["dev_before"], tlog["dev_after"]
        print(f"dev Hit@1 {b['hit@1']:.3f} -> {a['hit@1']:.3f}, MRR@10 {b['mrr@10']:.3f} -> {a['mrr@10']:.3f}" + ("" if tlog["beat_untuned_on_dev"] else "  (no checkpoint beat the untuned model: untuned weights kept)"))
    return 0


def _report(args, kb):
    from .encoder import Encoder, pick_device
    from .evaluate import evaluate
    from .kb import load_queries

    run = Path(args.run)
    log = json.loads((run / "train_log.json").read_text())
    dev = load_queries(args.dev, kb) if args.dev else None
    test = load_queries(args.test, kb)
    device = pick_device(args.device)
    base = Encoder.load(log["config"]["base"]).to(device)
    tuned = Encoder.load(run / "model").to(device)
    return evaluate(kb, dev, test, base, tuned, {**log, "curve": log.get("curve", [])}, args.device, args.target_precision), dev, test


def cmd_evaluate(args) -> int:
    from .evaluate import save_report

    kb = _kb(args)
    report, _, _ = _report(args, kb)
    save_report(report, Path(args.run) / "eval")
    print((Path(args.run) / "eval" / "report.md").read_text())
    return 0


def cmd_export(args) -> int:
    from .evaluate import save_report
    from .export import export_package

    kb = _kb(args)
    report, dev, test = _report(args, kb)
    save_report(report, Path(args.run) / "eval")
    info = export_package(args.run, kb, args.out, dev, test.texts, report, name=Path(args.out).name, keep_fp32=args.keep_fp32, target_precision=args.target_precision)
    print(json.dumps({k: v for k, v in info.items() if k != "variants"}, indent=2))
    for label, v in info["variants"].items():
        print(f"  {label}: {v}")
    return 0


STAGES = {"reading": "Reading your FAQ", "loading_model": "Loading the base model", "starting": "Starting training", "mining": "Finding confusable entries",
          "early_stop": "Stopped early: no further gain", "checking": "Checking the result", "saving": "Saving your engine", "finished": "Done"}


def cmd_auto(args) -> int:
    """Just a FAQ file: generate the checks, train, report, and optionally export."""
    from .auto import export_run, run_auto

    shown: list[str] = []

    def on_event(e: dict) -> None:
        if e["event"] == "stage" and e["stage"] in STAGES:
            if not shown or shown[-1] != e["stage"]:
                print(f"- {STAGES[e['stage']]}", flush=True)
            shown.append(e["stage"])
        elif e["event"] == "eval":
            print(f"    step {e['step']:>4}  train loss {e['train_loss']:.3f}  dev right-first {e['dev']['hit@1']:.3f}", flush=True)

    report = run_auto(args.kb, args.out, on_event, args.device, base_key=args.base, seed=args.seed, heldout_path=args.heldout, floor=args.floor, direct=args.direct, finetune=not args.no_finetune)
    print("\n" + "\n".join(f"* {v}" for v in report["verdict"]))
    print(f"\nFull report: {Path(args.out) / 'report.md'}")
    if args.export:
        info = export_run(args.out, args.export, sources=list(args.kb) if isinstance(args.kb, (list, tuple)) else [args.kb], heldout=args.heldout)
        print(f"Package: {info['zip']} ({info['zip_mb']:.1f} MB zipped)")
        print(f"Try it: faqengine chat --package {args.export}")
    return 0


def cmd_heldout_convert(args) -> int:
    """Check a test-question file against the knowledge base and write it in the plain format the studio takes."""
    import json as _json

    from .heldout import load_heldout
    from .kb import load_kb

    kb = load_kb(args.kb)
    ho = load_heldout(args.heldout, kb, labels=_json.loads(Path(args.labels).read_text()) if args.labels else None)
    ho.write_csv(args.out, kb)
    c = ho.counts()
    print(f"{c['rows_read']} rows -> {c['unique_questions']} unique questions ({c['exact_duplicates_removed']} repeated removed, {c['removed_for_leakage']} dropped as near-copies of FAQ phrasings), "
          f"{c['abstentions']} abstentions, {c['expected_entries']} different entries expected; written to {args.out}")
    return 0


def cmd_studio(args) -> int:
    from .studio.server import serve

    serve(args.data, args.port, args.open)
    return 0


def cmd_compare(args) -> int:
    from .compare import compare

    c = compare(args.previous, args.improved, args.heldout, args.map, args.out, tuple(args.seeds), args.device, args.base, args.notes)
    print(f"\nReport: {Path(args.out) / 'comparison.md'}")
    h = c["paired"]["hit@1"]["previous_vs_improved_engine"]
    print(f"Hit@1 on the same held-out test questions: {100 * h['a']:.1f}% -> {100 * h['b']:.1f}% ({100 * h['diff']:+.1f} points, 95% interval {100 * h['low']:+.1f} to {100 * h['high']:+.1f})")
    return 0


def cmd_chat(args) -> int:
    from .export import _engine

    root = Path(args.package)
    if not (root / "runtime" / "engine.py").exists():
        raise SystemExit(f"error: {root} is not an exported package (no runtime/engine.py); make one with `faqengine export`")
    engine = _engine(root)
    verdict = {"direct": "ANSWER DIRECTLY: confident, no LLM needed", "llm": "ASK YOUR LLM with these entries: related, not certain",
               "none": "NOT IN THE FAQ: nothing is close enough"}
    th = engine.thresholds

    def ask(question: str) -> None:
        r = engine.answer(question, k=3)
        print(f"  {verdict[r['decision']]}   (confidence {r['confidence']:.2f})")
        for h in r["hits"]:
            print(f"    {h['score']:.2f}  {h['question']}")
        if r["decision"] != "none":
            print(f"  stored answer of the best match: {r['hits'][0]['answer'] or '(this entry has no answer)'}")

    direct = "never (too few held-out questions to prove it is safe)" if th.get("direct") is None else f"{th['direct']:.2f} or more"
    print(f"{len(engine.entries)} FAQ entries. Answers directly at: {direct}; hands to an LLM from {th['floor']:.2f}; below that it says it does not know.")
    if args.question:
        for q in args.question:
            print(f"> {q}")
            ask(q)
        return 0
    print("Type a question and press Enter. Press Enter on an empty line to quit.")
    while True:
        try:
            q = input("> ").strip()
        except EOFError:
            break
        if not q:
            break
        ask(q)
    return 0


def _client(args):
    from . import paraphrase as pp

    try:
        return pp.client_from_env(args.base_url, args.model, args.key_env, args.delay)
    except pp.LLMError as e:
        raise SystemExit(f"error: {e}")


def cmd_paraphrase(args) -> int:
    from . import paraphrase as pp
    from .kb import write_kb

    client = _client(args)
    try:
        if args.check:
            reply = client.complete("Reply with the single word OK.", temperature=0)
            print(f"reached {args.base_url} with model {args.model}; it replied {reply.strip()[:40]!r}")
            return 0
        if not (args.kb and args.out):
            raise SystemExit("error: --kb and --out are required (or use --check)")
        kb = _kb(args)
        cache = pp.Cache(args.cache or Path(args.out).with_suffix(".paraphrases.json"), args.model, args.n)
        stats = pp.generate(kb.entries, args.n, client.complete, cache)
    except pp.LLMError as e:
        raise SystemExit(f"error: {e}")
    write_kb(pp.expand(kb.entries, cache), args.out)
    print(f"{stats['entries']} entries ({stats['asked']} asked, {stats['cached']} from cache, {stats['short']} with fewer than {args.n} usable); wrote {args.out}")
    return 0


def cmd_bench_paraphrases(args) -> int:
    from . import bench
    from . import paraphrase as pp

    client = _client(args)
    data = bench.load_banking77(args.data_dir)
    try:
        cache = pp.Cache(args.out, args.model, args.n)
        for seed in args.seeds:
            sc = bench.build_scenario(data, seed, args.entries, 1, args.answer)
            stats = pp.generate(sc.kb.entries, args.n, client.complete, cache, progress=lambda m, s=seed: print(f"seed {s}:{m}", flush=True))
            print(f"seed {seed}: {stats}", flush=True)
    except pp.LLMError as e:
        raise SystemExit(f"error: {e} (progress is saved in {args.out}; rerun to continue)")
    print(f"done: {args.out}")
    return 0


def cmd_bench(args) -> int:
    from . import bench
    from .train import TrainConfig

    cfg = TrainConfig(base=args.base, **{k: v for k, v in (("lr", args.lr), ("n_aug", args.n_aug)) if v is not None})
    path = bench.run_grid(args.out, args.base, args.k, args.seeds, args.entries, args.answer, cfg, args.data_dir, args.paraphrases, args.use)
    print(bench.summarize(path))
    return 0


def cmd_bench_summary(args) -> int:
    from . import bench

    print(bench.summarize(args.results))
    return 0


def main(argv: list[str] | None = None) -> int:
    from .encoder import BASES
    from .train import TrainConfig

    ap = argparse.ArgumentParser(prog="faqengine", description="Train, evaluate and export a custom FAQ retrieval engine.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    from .companion import cli as companion_cli

    companion_cli.build(sub)

    from .localapp import run as localapp_run

    localapp_run.build(sub)

    p = sub.add_parser("check", help="validate a FAQ file")
    p.add_argument("--kb", required=True, help="FAQ file (.csv, .json or .jsonl)")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("auto", help="from just a FAQ file: train, check and report (no dev or test files needed)")
    p.add_argument("--kb", required=True, help="FAQ file (.csv, .tsv, .xlsx, .json or .jsonl)")
    p.add_argument("--out", required=True, help="run folder")
    p.add_argument("--export", help="also write the portable package here, e.g. dist/my-faq")
    p.add_argument("--heldout", help="independent typed test questions (query,entry_id,type,split): evaluate on these instead of generated checks")
    p.add_argument("--floor", type=float, help="set the 'not in the FAQ' score line yourself instead of choosing it from the dev questions")
    p.add_argument("--direct", type=float, help="set the score from which answers may be given without an LLM (default: never until proven)")
    p.add_argument("--device", default="auto")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--base", choices=list(BASES), default="minilm-l6", help="the embedding model; multilingual-e5-small for text that is not English")
    p.add_argument("--no-finetune", action="store_true", help="use the pretrained model exactly as it is (the control to compare fine-tuning against)")
    p.set_defaults(fn=cmd_auto)

    p = sub.add_parser("heldout-convert", help="check a file of test questions against the knowledge base and write it in the plain format (repeats removed)")
    p.add_argument("--kb", nargs="+", required=True, help="the FAQ and document files the questions are about")
    p.add_argument("--heldout", required=True)
    p.add_argument("--labels", help="JSON mapping section names used in the file (\"FAQ: studio location\") to entry ids")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_heldout_convert)

    p = sub.add_parser("studio", help="the website: upload a FAQ, train, read the report, test, approve or reject")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", default="studio_data", help="where projects are kept")
    p.add_argument("--open", action="store_true", help="open the page in your browser")
    p.set_defaults(fn=cmd_studio)

    p = sub.add_parser("compare", help="previous version vs improved version of a FAQ, on the same independent held-out questions")
    p.add_argument("--previous", required=True, help="the previous FAQ file")
    p.add_argument("--improved", required=True, help="the improved FAQ file (may have alt_questions)")
    p.add_argument("--heldout", required=True, help="typed held-out questions written in terms of the improved FAQ's ids")
    p.add_argument("--map", required=True, help="CSV id,previous: which previous entries each improved entry absorbed (a|b for several)")
    p.add_argument("--notes", help="a markdown file appended to the report (assumptions, open questions)")
    p.add_argument("--out", required=True)
    p.add_argument("--seeds", type=_seeds, default=_seeds("0-2"))
    p.add_argument("--base", choices=list(BASES), default="minilm-l6")
    p.add_argument("--device", default="auto")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("train", help="fine-tune the embedding model on a FAQ file")
    p.add_argument("--kb", required=True)
    p.add_argument("--dev", help="CSV of held-out questions (query,entry_id) used to stop early and set thresholds")
    p.add_argument("--out", required=True, help="run folder")
    p.add_argument("--base", choices=list(BASES), default=TrainConfig.base)
    p.add_argument("--device", default="auto", help="auto, cpu, mps or cuda")
    p.add_argument("--lr", type=float)
    p.add_argument("--n-aug", dest="n_aug", type=int, help="style variants generated per question")
    p.add_argument("--max-steps", dest="max_steps", type=int)
    p.add_argument("--batch-size", dest="batch_size", type=int)
    p.add_argument("--seed", type=int)
    p.set_defaults(fn=cmd_train, hard_negatives=None)

    for name, fn, help_ in (("evaluate", cmd_evaluate, "report on unseen questions"), ("export", cmd_export, "write a portable engine package")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--run", required=True)
        p.add_argument("--kb", required=True)
        p.add_argument("--test", required=True, help="CSV of unseen questions (query,entry_id); blank entry_id marks out-of-scope")
        p.add_argument("--dev")
        p.add_argument("--device", default="auto")
        p.add_argument("--target-precision", dest="target_precision", type=float, default=0.95,
                       help="how often an answer given without asking an LLM must be right (0.95 = 19 in 20); needs enough --dev questions to prove it")
        if name == "export":
            p.add_argument("--out", required=True, help="package folder, e.g. dist/my-business")
            p.add_argument("--keep-fp32", action="store_true", help="ship the fp32 model as well as the chosen one")
        p.set_defaults(fn=fn)

    p = sub.add_parser("chat", help="try an exported package by typing questions")
    p.add_argument("--package", required=True, help="folder made by `export`, e.g. dist/bike-shop")
    p.add_argument("question", nargs="*", help="questions to ask once; leave empty to type them interactively")
    p.set_defaults(fn=cmd_chat)

    p = sub.add_parser("bench", help="benchmark fine-tuning against baselines on Banking77")
    p.add_argument("--out", default="runs/bench/results.jsonl")
    p.add_argument("--base", choices=list(BASES), default="minilm-l6")
    p.add_argument("--k", type=_ks, default=_ks("1,2,4,8"), help="phrasings per FAQ entry, comma separated; 'all' = every train question")
    p.add_argument("--seeds", type=_seeds, default=_seeds("0-4"))
    p.add_argument("--entries", type=int, default=30, help="FAQ entries per run")
    p.add_argument("--answer", choices=["label", "none"], default="label", help="answer text: the intent name, or nothing")
    p.add_argument("--lr", type=float)
    p.add_argument("--n-aug", dest="n_aug", type=int)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--paraphrases", help="cache from bench-paraphrases: each entry keeps one real phrasing (use --k 1) and gains LLM-written ones")
    p.add_argument("--use", type=int, help="use only the first N cached LLM paraphrases per entry")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("bench-summary", help="tabulate one or more results files")
    p.add_argument("results", nargs="+")
    p.set_defaults(fn=cmd_bench_summary)

    def llm_args(p) -> None:
        from .paraphrase import DEFAULT_BASE_URL, DEFAULT_KEY_ENV, DEFAULT_MODEL

        p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="any OpenAI-compatible endpoint: Gemini (default), Groq, OpenRouter, a local Ollama at http://localhost:11434/v1")
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="name of the environment variable holding the API key (never pass the key itself)")
        p.add_argument("--delay", type=float, default=4.0, help="minimum seconds between calls; free tiers rate-limit")

    p = sub.add_parser("paraphrase", help="add LLM-written phrasings to every FAQ entry (needs an API key in the environment)")
    p.add_argument("--kb", help="FAQ file")
    p.add_argument("--out", help="expanded FAQ file to write (.csv or .json)")
    p.add_argument("--n", type=int, default=5, help="phrasings per entry")
    p.add_argument("--cache", help="where generated phrasings are saved so a rerun repeats no call (default: next to --out)")
    p.add_argument("--check", action="store_true", help="only test that the endpoint, model and key work")
    llm_args(p)
    p.set_defaults(fn=cmd_paraphrase)

    p = sub.add_parser("bench-paraphrases", help="generate the LLM phrasings the benchmark's k=1 entries need")
    p.add_argument("--out", default="data/paraphrases.json")
    p.add_argument("--seeds", type=_seeds, default=_seeds("0-2"))
    p.add_argument("--entries", type=int, default=30)
    p.add_argument("--n", type=int, default=7, help="phrasings per entry (7 plus the real one makes 8)")
    p.add_argument("--answer", choices=["label", "none"], default="label")
    p.add_argument("--data-dir", default="data")
    llm_args(p)
    p.set_defaults(fn=cmd_bench_paraphrases)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
