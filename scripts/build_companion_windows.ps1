<#
Builds the Windows x64 companion and its setup program. Run it ON WINDOWS (PyInstaller cannot build a Windows program from another system):

  powershell -ExecutionPolicy Bypass -File scripts\build_companion_windows.ps1 -Origin https://your-site.vercel.app [-Version 0.1.0]

Needs: Python 3.11+ and Inno Setup 6 (https://jrsoftware.org/isinfo.php; `choco install innosetup`).
Optional signing: set KNOVENT_SIGN_THUMBPRINT to the SHA1 thumbprint of a code signing certificate that is ALREADY in your Windows certificate store (Cert:\CurrentUser\My). Nothing is
stored in this script or in the repository. Unset = an UNSIGNED build: Windows SmartScreen will warn "Windows protected your PC" until the setup program is signed with a trusted certificate.
Output: dist\Knovent-Companion-Setup-<version>-windows-x64.exe and a .sha256 file.
#>
param([Parameter(Mandatory = $true)][string[]]$Origin, [string]$Version = "")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not $Version) { $Version = (Select-String -Path "src\faqengine\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value }
$cfg = "src\faqengine\companion\build_config.json"
(@{ origins = @($Origin | ForEach-Object { $_.TrimEnd("/") }) } | ConvertTo-Json -Compress) | Set-Content -Encoding utf8 $cfg
try {
  python -m venv build\companion-win
  & build\companion-win\Scripts\python.exe -m pip install -q pyinstaller
  Remove-Item -Recurse -Force dist\knovent-companion -ErrorAction SilentlyContinue
  & build\companion-win\Scripts\pyinstaller.exe --noconfirm --clean --onedir --windowed --name knovent-companion --paths src `
      --distpath dist --workpath build\companion-win\work --specpath build\companion-win `
      --add-data "$PWD\src\faqengine\companion\catalogue.json;faqengine/companion" --add-data "$PWD\$cfg;faqengine/companion" scripts\companion_entry.py
} finally { Remove-Item -Force $cfg -ErrorAction SilentlyContinue }
$sign = { param($f) if ($env:KNOVENT_SIGN_THUMBPRINT) { & signtool sign /sha1 $env:KNOVENT_SIGN_THUMBPRINT /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f; if ($LASTEXITCODE) { throw "signing failed" } } }
& $sign "dist\knovent-companion\knovent-companion.exe"
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
& $iscc "/DAppVersion=$Version" "/DSourceDir=$PWD\dist\knovent-companion" scripts\windows\knovent.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }
$setup = "dist\Knovent-Companion-Setup-$Version-windows-x64.exe"
& $sign $setup
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
"$hash  $(Split-Path $setup -Leaf)" | Set-Content -Encoding ascii "$setup.sha256"
$sig = (Get-AuthenticodeSignature $setup).Status
Write-Host ""; Write-Host "built:      $setup"; Write-Host "version:    $Version (x64)"; Write-Host "signature:  $sig  $(if ($sig -ne 'Valid') {'(unsigned or untrusted: SmartScreen will warn; see KNOVENT_SIGN_THUMBPRINT)'})"; Write-Host "checksum:   $setup.sha256"
