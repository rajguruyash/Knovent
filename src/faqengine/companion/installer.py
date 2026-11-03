"""THE ONE-TIME INSTALLER for the companion (macOS). It does, in order, and tells the person what it is doing:

  1. copies the companion to a stable place (~/Library/Application Support/Knovent), so the login service does not depend on where the download was opened from;
  2. registers a per-user LaunchAgent that starts it at every login and keeps it running, trusting ONLY the website addresses it was built for;
  3. checks that it answers (a health check), and says what to do if it does not;
  4. looks for the AI runtime (Ollama); if it is missing it ASKS before downloading it from ollama.com, checks its signature, and puts it in the person's own Applications folder.

Models are not handled here: the website shows the person the recommended model, its size and the free space, and downloads it only after they agree (and not at all if one is installed).
Everything is a step with a plain result; every question to the person goes through `ui`. Nothing needs administrator rights, nothing runs a shell command the person typed, and nothing leaves this
computer except the one download the person approved. Windows and Linux are NOT implemented (docs/local-companion.md)."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .cli import LABEL
from .lifecycle import find_ollama
from .platforms import PlatformAdapter, adapter_for

OLLAMA_URL = "https://ollama.com/download/Ollama-darwin.zip"
OLLAMA_HOSTS = ("ollama.com",)
SETTLE = 1.0  # seconds the system is given between removing and adding a service registration
APP_DIR = Path("~/Library/Application Support/Knovent").expanduser()


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    ok: bool = True
    steps: list[Step] = field(default_factory=list)
    error: str = ""  # what went wrong and what to do about it, in plain words

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append(Step(name, ok, detail))
        if not ok:
            self.ok, self.error = False, self.error or detail
        return ok

    def summary(self) -> str:
        lines = [("done:   " if s.ok else "FAILED: ") + s.name + (f" ({s.detail})" if s.detail else "") for s in self.steps]
        return "\n".join(lines + ([] if self.ok else ["", self.error]))


class UI:
    """What the installer asks and shows. `confirm` is the only way a download or an install is ever approved."""

    def confirm(self, title: str, text: str, ok: str = "Continue", cancel: str = "Cancel") -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def progress(self, text: str) -> None:  # pragma: no cover - interface
        pass

    def info(self, title: str, text: str) -> None:  # pragma: no cover - interface
        pass


class ConsoleUI(UI):
    def __init__(self, yes: bool = False):
        self.yes = yes

    def confirm(self, title, text, ok="Continue", cancel="Cancel") -> bool:
        print(f"{title}\n{text}")
        return self.yes or input(f"{ok}? [y/N] ").strip().lower() in ("y", "yes")

    def progress(self, text):
        print("  ..." + text)

    def info(self, title, text):
        print(f"{title}: {text}")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 60), **kw)


def _health(port: int, wait: float = 20.0) -> str | None:
    """The companion's version if it answers on this computer within `wait` seconds."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/hello", timeout=2) as r:
                d = json.loads(r.read())
                if d.get("companion"):
                    return str(d.get("version", ""))
        except Exception:  # noqa: BLE001 (not up yet)
            time.sleep(0.4)
    return None


def bundle_of(exe: Path) -> Path | None:
    """The .app bundle a program runs from, if it is inside one (a PyInstaller onedir app: the executable needs the libraries around it, so the WHOLE bundle is installed, signature intact)."""
    for p in exe.parents:
        if p.suffix == ".app":
            return p
    return None


def _install_program(app_dir: Path, run=_run) -> Path:
    """Puts the running companion in its stable place and returns the program to start. A bundle is copied whole with `ditto` (it keeps the code signature and extended attributes of a signed app;
    `--noqtn` leaves out the download quarantine flag, because the person has already opened this app and approved it: launchd would otherwise refuse the copy silently). A single file is copied as before."""
    exe = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve()
    bundle = bundle_of(exe)
    if bundle is not None:
        dest, stage = app_dir / bundle.name, app_dir / (bundle.name + ".new")
        shutil.rmtree(stage, ignore_errors=True)
        done = run(["ditto", "--noqtn", str(bundle), str(stage)])
        if done.returncode:
            shutil.rmtree(stage, ignore_errors=True)
            shutil.copytree(bundle, stage, symlinks=True)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(stage, dest)
        return dest / exe.relative_to(bundle)
    try:  # already running from its install folder (the Windows setup program put it there): nothing to copy
        if app_dir.resolve() in exe.resolve().parents:
            return exe
    except OSError:
        pass
    if (exe.parent / "_internal").is_dir():  # a PyInstaller onedir folder (Windows): the program needs the libraries beside it, so the whole folder is installed
        dest, stage = app_dir / "app", app_dir / "app.new"
        shutil.rmtree(stage, ignore_errors=True)
        shutil.copytree(exe.parent, stage)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(stage, dest)
        return dest / exe.name
    dest = app_dir / "knovent-companion"
    tmp = dest.with_suffix(".new")
    shutil.copy2(exe, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    return dest


def _adapter() -> PlatformAdapter | None:
    try:
        return adapter_for()
    except NotImplementedError:
        return None


def agent_path(label: str = LABEL) -> Path:
    a = _adapter()
    return a.service_path(label) if a else Path.home() / f"{label}.service"


def status(port: int = 47890, label: str = LABEL) -> dict:
    """What is true now, for the installer's own start screen and for support: nothing here changes anything."""
    return {"service_registered": agent_path(label).exists(), "answering": _health(port, 1.0) is not None, "runtime_installed": find_ollama() is not None}


def install(origins: list[str], port: int = 47890, ui: UI | None = None, program_args: list[str] | None = None, label: str = LABEL, app_dir: Path | None = None, run=_run,
            ollama_installer=None, health_wait: float = 20.0, adapter: PlatformAdapter | None = None, version: str = "") -> Result:
    ui = ui or ConsoleUI()
    res = Result()
    origins = [o.rstrip("/") for o in origins]
    adapter = adapter or _adapter()
    if adapter is None or adapter.name not in ("macos", "windows"):
        res.add("platform", False, "Local AI setup works on macOS and Windows. Linux is not supported yet.")
        return res
    where = "Mac" if adapter.name == "macos" else "computer"
    from .auth import ORIGIN

    if not origins or not all(ORIGIN.match(o) for o in origins):
        res.add("website address", False, "The installer was not built for a website address, so it cannot be trusted to pair. Download it again from your Knovent site.")
        return res
    if not ui.confirm(f"Set up local AI on this {where}?", f"Knovent will: install a small helper that starts when you log in, use the AI runtime Ollama (installing it only if you agree), and keep your questions and "
                      "documents on this computer. Models are downloaded only after you approve them on the Knovent website.", "Set up", "Cancel"):
        res.add("approval", False, "Setup was cancelled. Nothing was changed.")
        return res
    # 1. the program, in a stable place
    app_dir = app_dir or (APP_DIR if adapter.name == "macos" else adapter.app_dir())
    try:
        ui.progress("Installing the Knovent companion")
        if program_args is None:
            app_dir.mkdir(parents=True, exist_ok=True)
            program_args = [str(_install_program(app_dir, run))]
        res.add("install the companion", True, program_args[0])
    except OSError as e:
        res.add("install the companion", False, f"The companion could not be copied ({e.strerror or e}). Check that your disk is not full and try again.")
        return res
    # 2. the login service (the adapter says what to write and which commands register it)
    svc = adapter.service_path(label)
    try:
        ui.progress("Starting it at every login")
        svc.parent.mkdir(parents=True, exist_ok=True)
        adapter.log_path().parent.mkdir(parents=True, exist_ok=True)
        text = adapter.service_text(program_args, origins, port, label)
        svc.write_bytes(text.encode(adapter.service_encoding))
        if adapter.name == "macos":
            plistlib.loads(svc.read_bytes())  # it must parse: launchd refuses a broken file without saying why
        failed = None
        for attempt in range(3):  # an upgrade replaces a service that is still being torn down: wait, register, and VERIFY it is loaded before saying it is
            failed = None
            for cmd, required in adapter.register_steps(label):
                done = run(cmd)
                if done.returncode and required:
                    if adapter.name == "macos" and cmd[1] == "bootstrap":
                        done = run(["launchctl", "load", "-w", str(svc)])
                    if done.returncode:
                        failed = done
                if not required:
                    time.sleep(SETTLE)  # let the system finish removing the old registration before the new one is added
            loaded = run(adapter.status_command(label))
            if failed is None and loaded.returncode == 0:
                break
            if failed is None:
                failed = loaded
            time.sleep(SETTLE * (attempt + 1))
        else:
            pass
        if failed is not None:
            how = "Open System Settings > General > Login Items and allow Knovent, then run setup again." if adapter.name == "macos" else "Close and open the installer again, and make sure Task Scheduler is not disabled by your organisation."
            res.add("register the login service", False, "The login service was not accepted: " + ((failed.stderr or failed.stdout or "").strip()[:160] or "no reason given") + ". " + how)
            return res
        adapter.post_install(program_args, version)
        res.add("register the login service", True, "starts at login for your user only")
    except OSError as e:
        res.add("register the login service", False, f"The login service could not be written ({e.strerror or e}).")
        return res
    # 3. does it answer
    ui.progress("Checking that it is running")
    ver = _health(port, health_wait)
    if ver is None:
        res.add("health check", False, f"The companion did not answer. Restart your {where} and open the Knovent website again; if it still does not connect, send the file {adapter.log_path()} to support.")
        return res
    res.add("health check", True, f"companion {ver} is answering on this computer only")
    # 4. the AI runtime
    ui.progress("Looking for the AI runtime")
    if find_ollama():
        res.add("AI runtime", True, "Ollama is already installed; the companion starts it when needed")
    else:
        size = "about 200 MB" if adapter.name == "macos" else "about 1 GB"
        place = "your own Applications folder" if adapter.name == "macos" else "your own user folder"
        if ui.confirm("Install the AI runtime?", f"Knovent needs Ollama (free) to run a model on this {where}. It will be downloaded from ollama.com ({size}), its signature checked, and placed in {place}. "
                      "No administrator password is needed.", "Download and install", "Not now"):
            fn = ollama_installer or (install_ollama if adapter.name == "macos" else install_ollama_windows)
            ok, detail = fn(ui)
            res.add("AI runtime", ok, detail)
        else:
            res.add("AI runtime", True, "not installed yet: the Knovent website will ask again when you choose local AI")
    return res


def install_ollama_windows(ui: UI, url: str = "https://ollama.com/download/OllamaSetup.exe", run=_run, opener=urllib.request.urlopen, workdir: Path | None = None) -> tuple[bool, str]:
    """Downloads Ollama's own Windows installer from ollama.com over https (the host is checked), refuses it unless Windows reports a VALID Authenticode signature whose signer is Ollama, and then runs it
    silently for this user (it installs under %LOCALAPPDATA%, no administrator rights). Called only after the person approved it. NOT yet run on Windows."""
    from urllib.parse import urlparse

    if urlparse(url).hostname != "ollama.com" or urlparse(url).scheme != "https":
        return False, "The download address is not Ollama's own, so it was refused."
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(workdir or tmp) / "OllamaSetup.exe"
        try:
            ui.progress("Downloading Ollama")
            with opener(url, timeout=60) as r, open(exe, "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
        except OSError as e:
            return False, f"Ollama could not be downloaded ({e}). Check your internet connection and try again."
        check = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", f"$s = Get-AuthenticodeSignature -LiteralPath '{exe}'; if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'Ollama') {{ 'VALID' }} else {{ 'REFUSED ' + $s.Status }}"])
        if "VALID" not in (check.stdout or "") or check.returncode:
            return False, "Windows would not confirm that the download is signed by Ollama, so it was not installed."
        ui.progress("Installing Ollama")
        done = run([str(exe), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], timeout=900)
        if done.returncode:
            return False, "The Ollama installer did not finish. Install it yourself from ollama.com, then try Knovent again."
    return True, "Ollama installed for your user"


def install_ollama(ui: UI, url: str = OLLAMA_URL, dest: Path | None = None, run=_run, opener=urllib.request.urlopen) -> tuple[bool, str]:
    """Downloads Ollama from ollama.com (the host is checked), refuses it unless macOS accepts its signature, and unpacks it into ~/Applications. Called only after the person approved it."""
    from urllib.parse import urlparse

    if urlparse(url).hostname not in OLLAMA_HOSTS or urlparse(url).scheme != "https":
        return False, "The download address is not Ollama's own, so it was refused."
    dest = dest or Path.home() / "Applications"
    with tempfile.TemporaryDirectory() as tmp:
        zpath = Path(tmp) / "Ollama.zip"
        try:
            ui.progress("Downloading Ollama")
            with opener(url, timeout=60) as r, open(zpath, "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
            with zipfile.ZipFile(zpath) as z:
                if any(n.startswith("/") or ".." in Path(n).parts for n in z.namelist()):
                    return False, "The download contained unsafe paths and was refused."
                z.extractall(tmp)
        except (OSError, zipfile.BadZipFile) as e:
            return False, f"Ollama could not be downloaded ({e}). Check your internet connection and try again."
        app = next(Path(tmp).glob("**/Ollama.app"), None)
        if app is None:
            return False, "The download did not contain Ollama.app and was refused."
        check = run(["spctl", "--assess", "--type", "execute", str(app)])
        if check.returncode:
            return False, "macOS would not accept Ollama's signature, so it was not installed."
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / "Ollama.app"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(app, target, symlinks=True)
    return True, f"Ollama installed in {dest}"


def uninstall(ui: UI | None = None, label: str = LABEL, app_dir: Path | None = None, run=_run, adapter: PlatformAdapter | None = None) -> Result:
    """Takes the login service and the companion away. Ollama and every downloaded model stay: they are not Knovent's to delete."""
    ui = ui or ConsoleUI()
    res = Result()
    adapter = adapter or _adapter()
    if adapter is None:
        res.add("platform", False, "Nothing to remove on this system.")
        return res
    for cmd in adapter.unregister_commands(label):
        run(cmd)
    svc = adapter.service_path(label)
    existed = svc.exists()
    svc.unlink(missing_ok=True)
    adapter.post_uninstall()
    target = app_dir or (APP_DIR if adapter.name == "macos" else adapter.app_dir())
    if label == LABEL or app_dir:
        adapter.remove_files(target)
    res.add("remove the login service", True, "removed" if existed else "it was not installed")
    res.add("keep Ollama and models", True, "left in place")
    return res
