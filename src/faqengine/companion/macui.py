"""The installer's questions as ordinary macOS dialogs (through `osascript`, which every Mac has). No Terminal, no extra software. Text is passed as data, never joined into the script as code."""
from __future__ import annotations

import subprocess

from .installer import UI


def _osa(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=3600)


class MacDialogUI(UI):
    def confirm(self, title: str, text: str, ok: str = "Continue", cancel: str = "Cancel") -> bool:
        r = _osa('on run argv\n display dialog (item 2 of argv) with title (item 1 of argv) buttons {(item 4 of argv), (item 3 of argv)} default button (item 3 of argv) with icon note\nend run', title, text, ok, cancel)
        return r.returncode == 0 and ok in r.stdout

    def choose(self, title: str, text: str, buttons: list[str]) -> str:
        r = _osa('on run argv\n display dialog (item 2 of argv) with title (item 1 of argv) buttons {(item 3 of argv), (item 4 of argv), (item 5 of argv)} default button (item 3 of argv)\nend run', title, text, *buttons[:3])
        for b in buttons:
            if r.returncode == 0 and f"button returned:{b}" in r.stdout:
                return b
        return buttons[0]

    def progress(self, text: str) -> None:
        _osa('on run argv\n display notification (item 1 of argv) with title "Knovent"\nend run', text)

    def info(self, title: str, text: str) -> None:
        _osa('on run argv\n display dialog (item 2 of argv) with title (item 1 of argv) buttons {"OK"} default button "OK"\nend run', title, text)
