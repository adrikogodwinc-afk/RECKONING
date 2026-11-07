; Inno Setup script. Packages the PyInstaller output into one setup file.
[Setup]
AppName=TradingOps
AppVersion=0.1.0
DefaultDirName={autopf}\TradingOps
DefaultGroupName=TradingOps
OutputDir=Output
OutputBaseFilename=TradingOps-Setup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin

[Files]
Source: "..\dist\TradingOps\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\TradingOps"; Filename: "{app}\TradingOps.exe"
Name: "{commonstartup}\TradingOps"; Filename: "{app}\TradingOps.exe"

[Run]
Filename: "{app}\TradingOps.exe"; Description: "Start TradingOps"; Flags: nowait postinstall skipifsilent
