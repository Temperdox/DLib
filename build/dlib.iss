; DLib installer — Inno Setup 6 script.
;
; To build:  open this file in Inno Setup Compiler and press F9,
;            or run on the command line:
;            "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\dlib.iss
;
; Output:    build\dist\Setup-DLib-0.1.12.exe
;
; Inno Setup is free: https://jrsoftware.org/isdl.php
;
; Installs to %LOCALAPPDATA%\Programs\DLib by default (no admin required).
; User data (db.sqlite3 + media/) lives separately under %LOCALAPPDATA%\DLib
; and is NOT removed by the uninstaller, so reinstalling preserves the library.

#define MyAppName "DLib"
#define MyAppVersion "0.1.12"
#define MyAppPublisher "DLib"
#define MyAppExeName "DLib.exe"

[Setup]
AppId={{D110B0D5-1A4A-4D5B-9C44-DA51716F84B5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=Setup-DLib-{#MyAppVersion}
SetupIconFile=dlib.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Pulls in everything Nuitka produced — exe, DLLs, templates, static, migrations.
Source: "dist\DLib.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
