#!/bin/zsh
# Runs the model comparison on the frozen fresh evidence. Needs, in the environment (never on the command line, never in the repository):
#   CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN       for the three Cloudflare models
#   OPENROUTER_API_KEY                                for the two OpenRouter models
# Every run saves each answered question at once (resume-safe: run the script again to continue). Prices are USD per million tokens as listed on the providers' pages on 2026-11-01
# (Cloudflare: developers.cloudflare.com/workers-ai/models/<model>; OpenRouter: openrouter.ai/api/v1/models) and are re-checked before use.
cd "$(dirname "$0")/.."
export HF_HUB_OFFLINE=1
E=runs/llm/evidence.fresh.json
PY=.venv/bin/python
run() { # label provider model price_in price_out [extra json]
  local out="runs/llm/fresh.$1.json"
  [[ -s $out ]] && { echo "skip $1 (done)"; return; }
  $PY scripts/llm_bench.py run --evidence $E --provider $2 --model "$3" --price-in $4 --price-out $5 --resume --json $out ${6:+--extra "$6"} > /dev/null || echo "FAILED $1"
}
$PY scripts/llm_bench.py run --evidence $E --deterministic --json runs/llm/fresh.deterministic.json > /dev/null
if [[ -n $CLOUDFLARE_ACCOUNT_ID && -n $CLOUDFLARE_API_TOKEN ]]; then
  run cf-gemma-4-26b cloudflare @cf/google/gemma-4-26b-a4b-it 0.10 0.30
  run cf-mistral-small-3.1 cloudflare @cf/mistralai/mistral-small-3.1-24b-instruct 0.351 0.555
  run cf-qwen3-30b-a3b cloudflare @cf/qwen/qwen3-30b-a3b-fp8 0.0509 0.335
else echo "Cloudflare variables not set: Cloudflare runs skipped"; fi
if [[ -n $OPENROUTER_API_KEY ]]; then
  run or-ministral-3b openrouter mistralai/ministral-3b-2512 0.10 0.10
  run or-qwen3.5-9b openrouter qwen/qwen3.5-9b 0.17 0.25 '{"reasoning":{"enabled":false}}'
else echo "OPENROUTER_API_KEY not set: OpenRouter runs skipped"; fi
