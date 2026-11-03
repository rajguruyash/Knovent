# The companion on macOS, Windows and Linux

One shared core, one adapter per operating system. **Support is claimed only for what has been run on that system.**

| System | Built | Unit tested | Installed, paired, crash-recovered, upgraded and uninstalled on a real machine | Inference tested | Status |
|---|---|---|---|---|---|
| macOS Apple silicon (arm64) | yes (onedir `.app`, ad-hoc signed) | yes | **yes** (Apple M2, macOS 15.2, 2026-11-03: `scripts/platform_selftest.py`, 16 PASS, download and cancel NOT RUN, 3 MANUAL) | **yes** (granite3.3:2b through the companion) | **Supported for development builds; a distributable build needs an Apple Developer ID and notarization** |
| macOS Intel (x64) | no (needs an Intel Mac or a `macos-13` runner) | adapter only | no | no | not supported |
| Windows x64 | **not built**: PyInstaller cannot build a Windows program from another system; the build script and workflow exist | yes (faked Windows environment) | **no** | **no** | **NOT SUPPORTED until the workflow/self-test below passes on a real Windows machine** |
| Linux x64 | no | adapter descriptions only | no | no | not started (deliberately: after Windows passes) |

## What is shared and what is per-system

Shared and OS-neutral (`companion/`): the HTTP server, signed pairing, business and browser binding, key expiry, the model catalogue and recommendations, hardware detection, model download and cancellation, runtime supervision, the inference interface. Per system (`companion/platforms.py`, `installer.py`, the dialogs): where the program and logs live, the login service and the commands that register and remove it, where Ollama is installed and how it is installed, how a process is started detached, and which dialogs are shown. An adapter only *describes*; `installer.py` runs the commands through an injectable runner, which is what makes the logic testable from any machine.

## Windows design (implemented, not yet run on Windows)

* **Program:** PyInstaller onedir `knovent-companion.exe` (windowed, no console), installed for the current user under `%LOCALAPPDATA%\Knovent\app`. No Python is needed.
* **Setup program:** Inno Setup (`scripts/windows/knovent.iss`): per user, no administrator rights, no UAC prompt. After copying files it opens the companion's own setup, which asks before it registers anything or installs Ollama. Upgrades close the running companion first. Settings > Apps lists it; its uninstaller removes the logon task first and leaves Ollama and models alone.
* **Startup and recovery:** a per-user Task Scheduler task: at logon, restart on failure (1 minute, up to 999 times), plus a **5 minute repetition as a safety net** (`IgnoreNew`, so it never starts a second copy). The safety net exists because whether "restart on failure" fires for a killed process is exactly what a real Windows test has to show.
* **Ollama:** found at `%LOCALAPPDATA%\Programs\Ollama` or `%ProgramFiles%\Ollama`; otherwise, after the person approves, Ollama's own `OllamaSetup.exe` is downloaded from ollama.com over https, accepted only if Windows reports a **valid Authenticode signature whose signer is Ollama**, and run silently for the user. Supervised by the same `OllamaSupervisor` (detached process flags, no console window, never a second copy, stops only what it started).
* **Security:** unchanged: signed pairing, trusted-site origins only, keys bound to a business and expiring, a readable refusal for an untrusted site. The website cannot make the companion run anything: it can only call the companion's fixed endpoints.
* **Signing:** optional. `KNOVENT_SIGN_THUMBPRINT` names an Authenticode certificate already in the Windows certificate store; nothing is stored in the repository. Unsigned, Windows SmartScreen shows "Windows protected your PC" and the person chooses More info, then Run anyway (the website says so).

## How Windows earns "supported"

On a real Windows 10/11 x64 PC or the hosted runner: `Actions > Companion Windows > Run workflow` (`.github/workflows/companion-windows.yml`) builds the setup program, installs it silently with the real installer, and runs `scripts/platform_selftest.py`, which checks: install without Python and without Ollama, the logon task, health, pairing from the trusted site, wrong-origin rejection, a key refused for another business, the companion's own report of OS and architecture, Ollama installed and running, existing-model detection, model download with progress, cancellation, local inference, companion crash recovery, Ollama crash recovery with no duplicate, upgrade, uninstall (task and files gone, Ollama kept). Two things a runner cannot do are reported MANUAL and must be done once by a person: a **reboot** and a **log out and in** starting the companion by itself, and the SmartScreen first-run experience. Only when this report has no FAIL, and the manual items were done, may `TESTED["windows-x64"]` be set to True and the website's Windows download be published (`COMPANION_DOWNLOADS`).

## Website

`/api/config` returns `companionDownloads` (from the `COMPANION_DOWNLOADS` environment variable, a JSON object such as `{"macos-arm64": "https://...", "windows-x64": "https://..."}`; https only, only for systems an installer is built for). The page picks an installer from the detected system, offers a "My computer is a" selector (a browser cannot reliably say which chip a Mac has), shows a single download button only when that installer is configured, and after pairing compares the companion's own report of the system with the choice. Windows is offered only if a Windows address is configured.
