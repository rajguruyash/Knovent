"""Which kind of window the installer uses on this system: macOS dialogs, Windows (tkinter) windows, or, where neither can be shown, questions in the console."""
from __future__ import annotations

from .installer import ConsoleUI, UI
from .platforms import detect_platform


def select_ui(os_name: str | None = None) -> UI:
    os_name = os_name or detect_platform().os
    if os_name == "macos":
        from .macui import MacDialogUI

        return MacDialogUI()
    if os_name == "windows":
        try:
            from .tkui import TkUI

            return TkUI()
        except Exception:  # noqa: BLE001 (no tkinter or no desktop session: the console is the honest fallback)
            return ConsoleUI()
    return ConsoleUI()
