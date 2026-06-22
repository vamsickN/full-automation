; Inno Setup script for Continuity Studio (Windows installer)
; Compile:  ISCC.exe installer.iss
; Output:   installer_output\ContinuityStudio-Setup.exe
;
; Wraps the PyInstaller one-folder build (dist\ContinuityStudio\) into a real
; Setup.exe with Start-menu + desktop shortcuts. No Python or ffmpeg needed on
; the target machine. Bundles the WebView2 bootstrapper so a fresh Windows
; install gets the native window without needing Edge first.

#define MyAppName "Continuity Studio"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Continuity Studio"
#define MyAppExeName "ContinuityStudio.exe"

[Setup]
AppId={{B7A6C2E0-9F4D-4C1A-8E2B-CS0001CONTIN}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install needs no admin; switch to "admin" for all-users.
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer_output
OutputBaseFilename=ContinuityStudio-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; The entire PyInstaller one-folder output. recursesubdirs + createallsubdirs
; pulls in static/, ffmpeg/, the _internal DLLs, everything.
Source: "dist\ContinuityStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Bundled WebView2 Evergreen bootstrapper. Installs the runtime on first run if
; the user doesn't already have it (Edge ships with it; this is the safety net
; for fresh Windows installs). Tiny — ~1.7 MB.
Source: "installer_payload\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{app}\webview2"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; If WebView2 runtime isn't installed, run the bundled bootstrapper silently.
; The /silent /install flag is the documented Evergreen Bootstrapper switch.
Filename: "{app}\webview2\MicrosoftEdgeWebview2Setup.exe"; \
  Parameters: "/silent /install"; \
  Check: NeedInstallWebView2; \
  Flags: waituntilterminated; \
  StatusMsg: "Installing WebView2 runtime (one-time, ~5 MB)..."
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
function NeedInstallWebView2(): Boolean;
var
  Key: String;
begin
  // x64 first (our build is 64-bit), then x86 fallback. If either key has a
  // version value, WebView2 is already installed.
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Key) then
    Result := False
  else if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Key) then
    Result := False
  else if RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Key) then
    Result := False
  else
    Result := True;
end;