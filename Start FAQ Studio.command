#!/bin/zsh
# Double-click this file to start the studio and open it in your browser. Close the window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/faqengine ]; then
  echo "First run: setting things up (a few minutes, needs internet once)..."
  python3 -m venv .venv && .venv/bin/pip install -q -e . || { echo "Setup failed. Press Return to close."; read; exit 1; }
fi
exec .venv/bin/faqengine studio --open
