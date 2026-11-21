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
Name: "desktopicon"; Description: "РЎРѕР·РґР°С‚СЊ СЏСЂР»С‹Рє РЅР° СЂР°Р±РѕС‡РµРј СЃС‚РѕР»Рµ"

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\rmcl"; Filename: "{app}\rmcl.exe"
Name: "{autodesktop}\rmcl"; Filename: "{app}\rmcl.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\rmcl.exe"; Description: "Р—Р°РїСѓСЃС‚РёС‚СЊ rmcl"; Flags: nowait postinstall skipifsilent
