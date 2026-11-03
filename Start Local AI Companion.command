#!/bin/zsh
# Double-click this file to start the local AI companion on this computer. It lets the web application use a model that runs HERE, so your documents and questions stay on this computer.
# The application on this computer pairs with it automatically; a hosted site needs FAQENGINE_APP_ORIGIN=https://your-site (or a pairing code, valid five minutes, used once). Close the window (or press Ctrl+C) to stop it. It never installs or downloads a model
# by itself: the application asks you first ("Set up local AI for me"), and Ollama (the program that runs the model, https://ollama.com/download) is installed by you.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/faqengine ]; then
  echo "First run: setting things up (a few minutes, needs internet once)..."
  python3 -m venv .venv && .venv/bin/pip install -q -e . || { echo "Setup failed. Press Return to close."; read; exit 1; }
fi
# start Ollama too if it is installed and not already running (it is the program that runs the model)
if command -v ollama >/dev/null 2>&1 && ! curl -s -m 1 http://127.0.0.1:11434 >/dev/null 2>&1; then
  echo "Starting Ollama..."; (nohup ollama serve >/dev/null 2>&1 &); sleep 2
fi
.venv/bin/faqengine companion recommend
echo
echo "Starting the companion on 127.0.0.1 only (nothing outside this computer can reach it)."
exec .venv/bin/faqengine companion run
