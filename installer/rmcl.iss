; AppDir / OutDir can be passed from the command line (/DAppDir=... /DOutDir=...)
#ifndef AppDir
  #define AppDir "..\dist\rmcl"
#endif
#ifndef OutDir
  #define OutDir "output"
#endif

[Setup]
AppName=rmcl
AppVersion=0.2.0
DefaultDirName={autopf}\rmcl
DefaultGroupName=rmcl
OutputDir={#OutDir}
OutputBaseFilename=rmcl-setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\rmcl"; Filename: "{app}\rmcl.exe"
Name: "{autodesktop}\rmcl"; Filename: "{app}\rmcl.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\rmcl.exe"; Description: "Запустить rmcl"; Flags: nowait postinstall skipifsilent
