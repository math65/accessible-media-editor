#ifndef AppName
  #define AppName "Accessible Media Editor"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef AppDistDirName
  #define AppDistDirName "AccessibleMediaEditor"
#endif
#ifndef AppExeName
  #define AppExeName "AccessibleMediaEditor.exe"
#endif
#ifndef AppOutputBaseFilename
  #define AppOutputBaseFilename "AccessibleMediaEditor-Setup"
#endif
#ifndef AppId
  #define AppId "{{8EF4AA32-F74A-45FD-85C6-1E6DDC6D42AE}"
#endif
#ifndef AppInstallDirName
  #define AppInstallDirName "Accessible Media Editor"
#endif
#ifndef AppPublisher
  #define AppPublisher "Accessible Media Editor"
#endif

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppInstallDirName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir=..\dist
OutputBaseFilename={#AppOutputBaseFilename}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ShowLanguageDialog=yes
; Permet à une mise à jour silencieuse (/SILENT, lancée par l'updater) de fermer
; proprement l'app et de remplacer les fichiers même si une instance traîne encore.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[CustomMessages]
english.EditWithApp=Edit with Accessible Media Editor
french.EditWithApp=Éditer avec Accessible Media Editor

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#AppDistDirName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; Entrée « Éditer avec… » dans le menu contextuel de l'explorateur (fichiers et
; dossiers). Lance l'exe avec le chemin sélectionné (%1). Nettoyé à la désinstallation.
; Sous-clé distincte de celle d'AMC (AccessibleMediaConverter) : les deux menus
; contextuels coexistent tant qu'AMC et AME sont installés côte à côte.
Root: HKCR; Subkey: "*\shell\AccessibleMediaEditor"; ValueType: string; ValueName: ""; ValueData: "{cm:EditWithApp}"; Flags: uninsdeletekey
Root: HKCR; Subkey: "*\shell\AccessibleMediaEditor"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExeName}"; Flags: uninsdeletekey
Root: HKCR; Subkey: "*\shell\AccessibleMediaEditor\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Flags: uninsdeletekey
Root: HKCR; Subkey: "Directory\shell\AccessibleMediaEditor"; ValueType: string; ValueName: ""; ValueData: "{cm:EditWithApp}"; Flags: uninsdeletekey
Root: HKCR; Subkey: "Directory\shell\AccessibleMediaEditor"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExeName}"; Flags: uninsdeletekey
Root: HKCR; Subkey: "Directory\shell\AccessibleMediaEditor\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Flags: uninsdeletekey

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Relancer l'application après une mise à jour silencieuse (lancée par l'updater).
; runasoriginaluser : l'installeur tourne en admin (UAC), on relance l'app sous
; l'utilisateur courant et non élevé.
Filename: "{app}\{#AppExeName}"; Flags: nowait runasoriginaluser; Check: WizardSilent
