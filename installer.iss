; Inno Setup script for Continuity Studio (Windows installer)
; Compile:  ISCC.exe installer.iss
; Output:   installer_output\ContinuityStudio-Setup.exe
;
; Wraps the PyInstaller one-folder build (dist\ContinuityStudio\) into a real
; Setup.exe with Start-menu + desktop shortcuts. No Python or ffmpeg needed on
; the target machine.

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

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
