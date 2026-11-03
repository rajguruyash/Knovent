"""The installer's questions as ordinary desktop windows through tkinter (part of Python's standard library, bundled into the Windows program). Used on Windows; no Terminal and no extra software.
Text is shown as data. Every question that approves a download or an install is a real window the person answers."""
from __future__ import annotations

from .installer import UI


class TkUI(UI):
    def __init__(self):
        import tkinter
        from tkinter import messagebox

        self.tk, self.mb = tkinter, messagebox
        self.root = tkinter.Tk()
        self.root.withdraw()
        self.root.attributes("-topmost", True)
        self.status = None

    def confirm(self, title: str, text: str, ok: str = "Continue", cancel: str = "Cancel") -> bool:
        return bool(self.mb.askyesno(title, f"{text}\n\n{ok}?", parent=self.root))

    def choose(self, title: str, text: str, buttons: list[str]) -> str:
        tk, picked = self.tk, [buttons[0]]
        win = tk.Toplevel(self.root)
        win.title(title)
        win.attributes("-topmost", True)
        tk.Label(win, text=text, justify="left", wraplength=420, padx=18, pady=14).pack()
        row = tk.Frame(win, pady=10)
        row.pack()
        for b in buttons:
            tk.Button(row, text=b, width=12, command=lambda b=b: (picked.__setitem__(0, b), win.destroy())).pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self.root.wait_window(win)
        return picked[0]

    def progress(self, text: str) -> None:
        tk = self.tk
        if self.status is None:
            self.status = tk.Toplevel(self.root)
            self.status.title("Knovent setup")
            self.status.attributes("-topmost", True)
            self.label = tk.Label(self.status, text="", padx=24, pady=18, width=44, anchor="w")
            self.label.pack()
        self.label.config(text=text + "…")
        self.status.update()

    def info(self, title: str, text: str) -> None:
        if self.status is not None:
            self.status.destroy()
            self.status = None
        self.mb.showinfo(title, text, parent=self.root)
