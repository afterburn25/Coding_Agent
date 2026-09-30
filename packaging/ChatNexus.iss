#ifndef MyAppVersion
  #define MyAppVersion "0.6.0-dev"
#endif

#ifndef MyAppVersionNumeric
  #define MyAppVersionNumeric "0.6.0.0"
#endif

#ifndef SourceDir
  #define SourceDir "..\dist\ChatNexus"
#endif

#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif

#define MyAppName "Chat Nexus"
#define MyAppPublisher "Chat Nexus"
#define MyAppExeName "ChatNexus.exe"
#define MyAppId "{{4A4EC89B-8F5B-4F4F-A722-5B48F3B927A4}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersionNumeric}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoDescription=Chat Nexus local AI coding workstation installer
DefaultDirName={localappdata}\Programs\Chat Nexus
DefaultGroupName=Chat Nexus
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=Chat-Nexus-Setup-v{#MyAppVersion}
SetupIconFile={#SourceDir}\chat-nexus.ico
UninstallDisplayIcon={app}\ChatNexus.exe
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=force
RestartApplications=no
UsePreviousAppDir=yes
UsePreviousGroup=yes
CreateUninstallRegKey=yes
Uninstallable=yes
SetupLogging=yes
MinVersion=10.0.17763
ChangesAssociations=no
ChangesEnvironment=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Dirs]
Name: "{app}\models"
Name: "{app}\data"
Name: "{app}\workflows"
Name: "{app}\.agent"

[Files]
; Application files are replaced on install/upgrade.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "config.json,Source\*"; Flags: ignoreversion recursesubdirs createallsubdirs

; Keep an up-to-date example while preserving the user's real config.
Source: "{#SourceDir}\config.example.json"; DestDir: "{app}"; DestName: "config.example.json"; Flags: ignoreversion
Source: "{#SourceDir}\config.example.json"; DestDir: "{app}"; DestName: "config.json"; Flags: onlyifdoesntexist

; The self-development Git workspace is seeded once and then preserved on upgrades.
Source: "{#SourceDir}\Source\*"; DestDir: "{app}\Source"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: ShouldInstallSource

[Icons]
Name: "{group}\Chat Nexus"; Filename: "{app}\ChatNexus.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Chat Nexus"; Filename: "{app}\ChatNexus.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ChatNexus.exe"; Description: "Launch Chat Nexus"; Flags: nowait postinstall skipifsilent

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{4A4EC89B-8F5B-4F4F-A722-5B48F3B927A4}_is1';

function ReadInstalledVersion(var Version: String): Boolean;
begin
  Result :=
    RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', Version) or
    RegQueryStringValue(HKLM, UninstallKey, 'DisplayVersion', Version) or
    RegQueryStringValue(HKLM64, UninstallKey, 'DisplayVersion', Version);
end;

function InitializeSetup(): Boolean;
var
  InstalledVersion: String;
  Choice: Integer;
begin
  Result := True;

  if ReadInstalledVersion(InstalledVersion) then
  begin
    if WizardSilent then
    begin
      Log('Existing Chat Nexus ' + InstalledVersion + ' detected; silent upgrade will continue.');
    end
    else
    begin
      Choice := MsgBox(
        'Chat Nexus ' + InstalledVersion + ' is already installed.' + #13#10 + #13#10 +
        'Upgrade this installation to Chat Nexus {#MyAppVersion}?' + #13#10 + #13#10 +
        'Your downloaded models, config, generated data, and existing Source Git workspace will be preserved.',
        mbConfirmation,
        MB_YESNO
      );

      if Choice <> IDYES then
      begin
        Result := False;
        Exit;
      end;
    end;
  end;
end;

function ShouldInstallSource(): Boolean;
begin
  Result := not DirExists(ExpandConstant('{app}\Source\.git'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    Log('Chat Nexus install/upgrade completed. User data and model folders were preserved.');
  end;
end;
