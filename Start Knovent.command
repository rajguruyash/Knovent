#!/bin/zsh
# Double-click to start Knovent on this computer: the website, the real engine, the local-model companion and Ollama (if installed), all in one. Press Ctrl+C in this window to stop.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/faqengine ]; then
  echo "First run: setting things up (a few minutes, needs internet once)..."
  python3 -m venv .venv && .venv/bin/pip install -q -e . || { echo "Setup failed. Press Return to close."; read; exit 1; }
fi
exec .venv/bin/faqengine app
