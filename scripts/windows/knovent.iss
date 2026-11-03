; Knovent Companion: the Windows setup program (Inno Setup 6). It installs for the CURRENT USER only (no administrator rights, no UAC prompt) into %LOCALAPPDATA%\Knovent\app,
; then starts the companion's own setup, which asks before it registers the logon task or installs anything else. Nothing here runs without the person opening it.
;   iscc /DAppVersion=0.1.0 /DSourceDir=dist\knovent-companion scripts\windows\knovent.iss      (see scripts\build_companion_windows.ps1)
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\knovent-companion"
#endif
[Setup]
AppId={{6B0F3A52-4C21-4E0B-9A6F-5D3E1C7A9B84}
AppName=Knovent Companion
AppVersion={#AppVersion}
AppPublisher=Knovent
DefaultDirName={localappdata}\Knovent\app
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=Knovent-Companion-Setup-{#AppVersion}-windows-x64
OutputDir=..\..\dist
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Knovent Companion
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Run]
; the companion's own setup: asks, registers the logon task, health-checks, offers Ollama (with approval). Skipped in a silent install (the self-test runs it with --yes).
Filename: "{app}\knovent-companion.exe"; Parameters: "setup --gui"; Flags: nowait runasoriginaluser skipifsilent

[UninstallRun]
; remove the logon task before the files go; Ollama and downloaded models are left alone
Filename: "{app}\knovent-companion.exe"; Parameters: "uninstall"; Flags: runhidden; RunOnceId: "KnoventUninstall"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  // an upgrade: stop the running companion so its files can be replaced (the logon task starts it again afterwards)
  Exec('schtasks.exe', '/End /TN ai.knovent.companion', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec('taskkill.exe', '/F /IM knovent-companion.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
