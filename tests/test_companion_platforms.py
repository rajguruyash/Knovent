"""The platform layer: which system this is, which adapter it gets, and exactly what each adapter would write and run. Nothing is executed: adapters only describe, so all three are testable from any machine.
This proves the descriptions are well formed. It does NOT prove a service starts on Windows or Linux: that needs those systems (docs/cross-platform-companion.md)."""
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from faqengine.companion import platforms as P

ORIGIN = "https://my-site.vercel.app"
LABEL = "ai.knovent.companion"


@pytest.mark.parametrize("sysplatform, machine, key", [("darwin", "arm64", "macos-arm64"), ("darwin", "x86_64", "macos-x64"), ("win32", "AMD64", "windows-x64"), ("linux", "x86_64", "linux-x64"),
                                                       ("linux", "aarch64", "linux-arm64"), ("win32", "ARM64", "windows-arm64"), ("freebsd14", "amd64", "other-x64")])
def test_the_system_and_architecture_are_recognised(sysplatform, machine, key):
    assert P.detect_platform(sysplatform, machine).key == key


def test_only_the_systems_the_installers_are_built_for_get_an_adapter():
    assert isinstance(P.adapter_for(P.Platform("macos", "arm64")), P.MacOSAdapter)
    assert isinstance(P.adapter_for(P.Platform("windows", "x64")), P.WindowsAdapter)
    assert isinstance(P.adapter_for(P.Platform("linux", "x64")), P.LinuxAdapter)
    with pytest.raises(NotImplementedError):
        P.adapter_for(P.Platform("other", "x64"))
    assert P.Platform("windows", "arm64").supported_target is False  # recognised, but no installer is built for it


def test_nothing_is_marked_tested_that_has_not_been_run_on_that_system():
    assert P.TESTED == {"macos-arm64": True, "macos-x64": False, "windows-x64": False, "linux-x64": False}


def test_macos_describes_a_launch_agent_in_the_users_own_library(tmp_path):
    a = P.MacOSAdapter(home=tmp_path)
    assert a.service_path(LABEL) == tmp_path / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert a.app_dir() == tmp_path / "Library" / "Application Support" / "Knovent"
    text = a.service_text(["/x/Knovent Companion"], [ORIGIN], 47890, LABEL)
    assert "<key>KeepAlive</key><true/>" in text and f"<string>{ORIGIN}</string>" in text
    cmds = a.register_commands(LABEL)
    assert [c[1] for c in cmds] == ["bootout", "bootstrap"]  # replaced first, never doubled
    assert any(p.endswith("ollama") for p in a.ollama_candidates()) and any("Homebrew" in p or "homebrew" in p for p in a.ollama_candidates())


def test_windows_describes_a_per_user_logon_task_that_restarts_and_needs_no_administrator():
    env = {"LOCALAPPDATA": r"C:\Users\Ann\AppData\Local", "USERNAME": "Ann", "USERDOMAIN": "PC", "ProgramFiles": r"C:\Program Files"}
    a = P.WindowsAdapter(env=env)
    xml = a.service_text([r"C:\Users\Ann\AppData\Local\Knovent\knovent-companion.exe"], [ORIGIN], 47890, LABEL)
    root = ET.fromstring(xml.split("?>", 1)[1])  # well formed XML
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:LogonTrigger", ns) is not None and root.find(".//t:RunLevel", ns).text == "LeastPrivilege"
    assert root.find(".//t:RestartOnFailure/t:Count", ns).text == "999"
    args = root.find(".//t:Exec/t:Arguments", ns).text
    assert args == f"run --port 47890 --origin {ORIGIN}"
    cmds = a.register_commands(LABEL)
    assert [c[1] for c in cmds] == ["/End", "/Create", "/Run"] and "/XML" in cmds[1]
    assert [c[1] for c in a.unregister_commands(LABEL)] == ["/End", "/Delete"]
    assert any(p.replace("\\", "/").endswith("Programs/Ollama/ollama.exe") for p in a.ollama_candidates())  # (separators follow the machine it runs on) and a.program_name().endswith(".exe")
    assert a.spawn_kwargs()["creationflags"] & 0x08000000  # no console window flashes at the person
    assert a.runtime_download("x64")[0].endswith("OllamaSetup.exe") and a.runtime_download("arm64") is None


def test_linux_describes_a_systemd_user_service_with_restart_and_no_root(tmp_path):
    a = P.LinuxAdapter(env={}, home=tmp_path)
    assert a.service_path(LABEL) == tmp_path / ".config" / "systemd" / "user" / f"{LABEL}.service"
    text = a.service_text(["/home/ann/.local/share/knovent/knovent-companion"], [ORIGIN], 47890, LABEL)
    assert "Restart=always" in text and "WantedBy=default.target" in text and f"--origin {ORIGIN}" in text
    assert [c[:3] for c in a.register_commands(LABEL)] == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable"]]
    assert all("sudo" not in " ".join(c) for c in a.register_commands(LABEL) + a.unregister_commands(LABEL))
    assert "Exec=" in a.autostart_text(["/x/c"], [ORIGIN], 47890, LABEL)  # the fallback where there is no user systemd
    assert a.runtime_download("x64")[0].endswith("ollama-linux-amd64.tgz") and a.runtime_download("arm64")[0].endswith("ollama-linux-arm64.tgz")


def test_runtime_candidates_are_not_assumed_to_be_the_same_everywhere():
    sets = {a.name: set(a.ollama_candidates()) for a in (P.MacOSAdapter(), P.WindowsAdapter(env={}), P.LinuxAdapter(env={}))}
    assert sets["macos"] != sets["windows"] != sets["linux"]
    assert all(not Path(p).is_absolute() or "/" in p or "\\" in p for s in sets.values() for p in s)
