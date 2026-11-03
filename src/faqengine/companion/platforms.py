"""THE PLATFORM LAYER. Everything that differs between operating systems lives here, behind one interface; the rest of the companion (server, signed pairing, business binding, models, hardware, downloads,
runtime supervision, inference) is shared and OS-neutral.

An adapter does not run anything itself. It DESCRIBES: where the program and logs live, the service definition, the exact commands that register, start, stop and remove it, where the model runtime is
usually installed, and how a runtime download is verified. `installer.py` runs those commands (through an injectable runner), which is what lets all three adapters be tested from any machine.

STATUS (what has actually been run, see docs/cross-platform-companion.md): macos-arm64 was installed, restarted and uninstalled on a real Mac. Every other adapter below is implemented and unit tested
and has NOT been run on that operating system: `TESTED` says so, and the website offers only what is marked supported."""
from __future__ import annotations

import os
import platform as _platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .cli import launch_agent_plist

INNO_APP_ID = "{6B0F3A52-4C21-4E0B-9A6F-5D3E1C7A9B84}"  # the Windows setup program's application id (scripts/windows/knovent.iss): one entry under Settings > Apps
TESTED = {"macos-arm64": True, "macos-x64": False, "windows-x64": False, "linux-x64": False}  # flipped to True only when the install and an inference test have passed on that system
ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64", "x64": "x64", "i386": "x86", "i686": "x86"}


@dataclass(frozen=True)
class Platform:
    os: str    # macos | windows | linux | other
    arch: str  # arm64 | x64 | x86 | other

    @property
    def key(self) -> str:
        return f"{self.os}-{self.arch}"

    @property
    def supported_target(self) -> bool:
        """A system the installers are built for (not the same as TESTED)."""
        return self.key in TESTED


def detect_platform(sysplatform: str | None = None, machine: str | None = None) -> Platform:
    sp = (sysplatform or sys.platform).lower()
    os_ = "macos" if sp == "darwin" else "windows" if sp in ("win32", "cygwin") else "linux" if sp.startswith("linux") else "other"
    return Platform(os_, ARCH.get((machine or _platform.machine()).lower(), "other"))


class PlatformAdapter:
    name = "other"
    RUN_ARGS_NOTE = ""

    def __init__(self, env: dict | None = None, home: Path | None = None):
        self.env = os.environ if env is None else env
        self.home = home or Path.home()

    # ---- where things are
    def app_dir(self) -> Path: raise NotImplementedError
    def log_path(self) -> Path: raise NotImplementedError
    def program_name(self) -> str: return "knovent-companion"
    def ollama_candidates(self) -> tuple[str, ...]: return ()
    def spawn_kwargs(self) -> dict: return {"start_new_session": True}

    # ---- the login service: a definition and the commands that act on it
    def service_path(self, label: str) -> Path: raise NotImplementedError
    def service_text(self, program_args: list[str], origins: list[str], port: int, label: str) -> str: raise NotImplementedError
    def register_commands(self, label: str) -> list[list[str]]: raise NotImplementedError
    def unregister_commands(self, label: str) -> list[list[str]]: raise NotImplementedError
    def stop_commands(self, label: str) -> list[list[str]]: return self.unregister_commands(label)
    def status_command(self, label: str) -> list[str]: raise NotImplementedError

    # ---- the model runtime (Ollama) this system uses, and how its download is checked
    def runtime_download(self, arch: str) -> tuple[str, tuple[str, ...]] | None: return None
    service_encoding = "utf-8"

    def register_steps(self, label: str) -> list[tuple[list[str], bool]]:
        """(command, must succeed) in order. A step that only tidies up an earlier install may fail harmlessly."""
        cmds = self.register_commands(label)
        return [(c, i == len(cmds) - 1) for i, c in enumerate(cmds)]

    def post_install(self, program_args: list[str], version: str) -> None: pass   # system-level bookkeeping after install (for example the Windows "Apps" entry)
    def post_uninstall(self) -> None: pass
    def remove_files(self, app_dir: Path) -> None:
        import shutil
        shutil.rmtree(app_dir, ignore_errors=True)


class MacOSAdapter(PlatformAdapter):
    name = "macos"

    def app_dir(self): return self.home / "Library" / "Application Support" / "Knovent"
    def log_path(self): return self.home / "Library" / "Logs" / "knovent-companion.log"
    def ollama_candidates(self): return ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama", str(self.home / "Applications/Ollama.app/Contents/Resources/ollama"))
    def service_path(self, label): return self.home / "Library" / "LaunchAgents" / f"{label}.plist"

    def service_text(self, program_args, origins, port, label):
        return launch_agent_plist(program_args, origins, port, str(self.log_path()), label)

    def register_commands(self, label):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        p = str(self.service_path(label))
        return [["launchctl", "bootout", f"gui/{uid}/{label}"], ["launchctl", "bootstrap", f"gui/{uid}", p]]  # the first replaces an earlier install, so it is never doubled

    def unregister_commands(self, label):
        return [["launchctl", "bootout", f"gui/{os.getuid() if hasattr(os, 'getuid') else 0}/{label}"]]

    def status_command(self, label): return ["launchctl", "list", label]
    def runtime_download(self, arch): return ("https://ollama.com/download/Ollama-darwin.zip", ("ollama.com",))

    def register_steps(self, label):
        out, boot = self.register_commands(label)
        return [(out, False), (boot, True)]


class WindowsAdapter(PlatformAdapter):
    """Per-user Task Scheduler task, started at logon and restarted if it fails: no administrator rights, no service account. (NOT yet run on Windows.)"""
    name = "windows"
    service_encoding = "utf-16"  # schtasks /XML wants UTF-16 with a byte order mark

    def app_dir(self): return Path(self.env.get("LOCALAPPDATA") or self.home / "AppData" / "Local") / "Knovent"
    def log_path(self): return self.app_dir() / "companion.log"
    def program_name(self): return "knovent-companion.exe"
    def spawn_kwargs(self): return {"creationflags": 0x00000008 | 0x00000200 | 0x08000000}  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    def ollama_candidates(self):
        local = self.env.get("LOCALAPPDATA") or str(self.home / "AppData" / "Local")
        pf = self.env.get("ProgramFiles") or r"C:\Program Files"
        return (str(Path(local) / "Programs" / "Ollama" / "ollama.exe"), str(Path(pf) / "Ollama" / "ollama.exe"))

    def service_path(self, label): return self.app_dir() / f"{label}.task.xml"

    def service_text(self, program_args, origins, port, label):
        exe, rest = program_args[0], [*program_args[1:], "run", "--port", str(port), "--replace", *[x for o in origins for x in ("--origin", o)]]
        args = " ".join(f'"{a}"' if re.search(r"\s", a) else a for a in rest)
        user = self.env.get("USERDOMAIN", "") + "\\" + self.env.get("USERNAME", "") if self.env.get("USERNAME") else ""
        return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Knovent local AI companion (this computer only)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled>{f'<UserId>{escape(user)}</UserId>' if user else ''}</LogonTrigger>
    <TimeTrigger><StartBoundary>2020-01-01T00:00:00</StartBoundary><Enabled>true</Enabled><Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger></Triggers>  <!-- the 5 minute repetition is a safety net: if the companion is ever not running, it is started again (a running one is left alone: IgnoreNew) -->
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Hidden>true</Hidden><RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure></Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>
"""

    def register_commands(self, label):
        return [["schtasks", "/End", "/TN", label], ["schtasks", "/Create", "/TN", label, "/XML", str(self.service_path(label)), "/F"], ["schtasks", "/Run", "/TN", label]]

    def register_steps(self, label):
        end, create, run = self.register_commands(label)
        return [(end, False), (create, True), (run, False)]  # ending an earlier copy may find nothing to end; starting it now is a convenience (it also starts at the next logon)

    def unregister_commands(self, label): return [["schtasks", "/End", "/TN", label], ["schtasks", "/Delete", "/TN", label, "/F"]]
    def status_command(self, label): return ["schtasks", "/Query", "/TN", label]
    UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\KnoventCompanion"

    def post_install(self, program_args, version):
        """Lists Knovent under Settings > Apps (per user, no administrator), with an uninstall that runs the companion's own uninstall."""
        if sys.platform != "win32":
            return
        import winreg

        try:  # the setup program (Inno) already lists Knovent under Apps: no second entry
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.UNINSTALL_KEY.replace("KnoventCompanion", INNO_APP_ID + "_is1")))
            return
        except OSError:
            pass
        exe = program_args[0]
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.UNINSTALL_KEY, 0, winreg.KEY_WRITE) as k:
            for name, val in (("DisplayName", "Knovent Companion"), ("DisplayVersion", version), ("Publisher", "Knovent"), ("InstallLocation", str(Path(exe).parent)),
                              ("UninstallString", f'"{exe}" uninstall --gui'), ("NoModify", 1), ("NoRepair", 1)):
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD if isinstance(val, int) else winreg.REG_SZ, val)

    def post_uninstall(self):
        if sys.platform != "win32":
            return
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.UNINSTALL_KEY)
        except OSError:
            pass

    def remove_files(self, app_dir):
        """A running program cannot delete its own folder on Windows: remove what can be removed now, and hand the rest to a detached cleanup that runs after this process has ended."""
        import shutil
        import subprocess

        shutil.rmtree(app_dir, ignore_errors=True)
        if app_dir.exists():
            subprocess.Popen(["cmd", "/c", f'ping -n 4 127.0.0.1 >nul & rmdir /s /q "{app_dir}"'], **self.spawn_kwargs(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def runtime_download(self, arch): return ("https://ollama.com/download/OllamaSetup.exe", ("ollama.com",)) if arch == "x64" else None


class LinuxAdapter(PlatformAdapter):
    """A systemd USER service (no root): enabled at login and restarted if it stops. Where there is no user systemd (some containers, WSL) an XDG autostart entry is used instead, which starts it at login
    but cannot restart it after a crash. (NOT yet run on Linux.)"""
    name = "linux"

    def app_dir(self): return Path(self.env.get("XDG_DATA_HOME") or self.home / ".local" / "share") / "knovent"
    def log_path(self): return self.app_dir() / "companion.log"
    def ollama_candidates(self): return ("/usr/local/bin/ollama", "/usr/bin/ollama", str(self.home / ".local/bin/ollama"), str(self.home / ".local/ollama/bin/ollama"))
    def service_path(self, label): return Path(self.env.get("XDG_CONFIG_HOME") or self.home / ".config") / "systemd" / "user" / f"{label}.service"

    def service_text(self, program_args, origins, port, label):
        args = [*program_args, "run", "--port", str(port), "--replace", *[x for o in origins for x in ("--origin", o)]]
        quoted = " ".join('"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r"[\s\"'\\]", a) else a for a in args)
        return f"""[Unit]
Description=Knovent local AI companion (this computer only)
After=network.target

[Service]
ExecStart={quoted}
Restart=always
RestartSec=3
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
StandardOutput=append:{self.log_path()}
StandardError=append:{self.log_path()}

[Install]
WantedBy=default.target
"""

    def register_commands(self, label):
        return [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{label}.service"]]

    def unregister_commands(self, label): return [["systemctl", "--user", "disable", "--now", f"{label}.service"], ["systemctl", "--user", "daemon-reload"]]
    def status_command(self, label): return ["systemctl", "--user", "is-active", f"{label}.service"]
    def autostart_path(self, label): return Path(self.env.get("XDG_CONFIG_HOME") or self.home / ".config") / "autostart" / f"{label}.desktop"

    def autostart_text(self, program_args, origins, port, label):
        args = [*program_args, "run", "--port", str(port), "--replace", *[x for o in origins for x in ("--origin", o)]]
        return f"[Desktop Entry]\nType=Application\nName=Knovent companion\nExec={' '.join(args)}\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n"

    def runtime_download(self, arch):
        return (f"https://ollama.com/download/ollama-linux-{'arm64' if arch == 'arm64' else 'amd64'}.tgz", ("ollama.com", "github.com"))


def adapter_for(p: Platform | None = None, **kw) -> PlatformAdapter:
    """The adapter for this system. An unknown system has none: callers say plainly that it is not supported instead of guessing."""
    p = p or detect_platform()
    cls = {"macos": MacOSAdapter, "windows": WindowsAdapter, "linux": LinuxAdapter}.get(p.os)
    if cls is None:
        raise NotImplementedError(f"{p.os} is not supported")
    return cls(**kw)
