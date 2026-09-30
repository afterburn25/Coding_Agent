#ifndef AppVersion
  #define AppVersion "0.6.0-dev"
#endif

#ifndef AppNumericVersion
  #define AppNumericVersion "0.6.0.0"
#endif

#define AppName "Chat Nexus"
#define AppPublisher "Afterburn25"
#define AppExeName "ChatNexus.exe"
#define StableAppId "ChatNexus.Afterburn25"

[Setup]
AppId={#StableAppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Chat Nexus
DefaultGroupName=Chat Nexus
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UsePreviousAppDir=yes
UsePreviousGroup=yes
OutputDir=..\dist\installer
OutputBaseFilename=Chat-Nexus-Setup-{#AppVersion}-Windows-x64
SetupIconFile=..\desktop\ChatNexus.Desktop\chat-nexus.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=ChatNexus.exe,ChatNexus.Backend.exe,llama-server.exe
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#AppNumericVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppNumericVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Chat Nexus Installer
VersionInfoCopyright=Chat Nexus
MinVersion=10.0.17763
ChangesEnvironment=no
ChangesAssociations=no
Uninstallable=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Replace application/runtime files on every install or upgrade, but never overwrite
; mutable user state or the bundled self-development Git workspace.
Source: "..\dist\ChatNexus\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "Source\*;models\*;data\*;config.json"

; Seed the self-development workspace only when it does not already exist.
; Existing Source/.git plus local edits are preserved during upgrades.
Source: "..\dist\ChatNexus\Source\*"; DestDir: "{app}\Source"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: ShouldInstallBundledSource

[Dirs]
Name: "{app}\models"
Name: "{app}\data"

[InstallDelete]
; Clean the obsolete pywebview portable runtime from early dogfood builds if an
; installer is pointed at that same directory. Mutable model/data/source paths are untouched.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\Chat Nexus"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\Chat Nexus"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch Chat Nexus"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
var
  UpgradeDetected: Boolean;
  ExistingVersion: String;
  ExistingInstallDir: String;
  UpgradeInfoPage: TOutputMsgWizardPage;
  InstallBundledSource: Boolean;

function InstalledUninstallKey(): String;
begin
  Result := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#StableAppId}_is1';
end;

function DetectExistingInstall(): Boolean;
var
  Key: String;
  DefaultPath: String;
begin
  Result := False;
  ExistingVersion := '';
  ExistingInstallDir := '';
  Key := InstalledUninstallKey();

  if RegQueryStringValue(HKCU, Key, 'DisplayVersion', ExistingVersion) then
  begin
    RegQueryStringValue(HKCU, Key, 'InstallLocation', ExistingInstallDir);
    Result := True;
    Exit;
  end;

  if RegQueryStringValue(HKLM, Key, 'DisplayVersion', ExistingVersion) then
  begin
    RegQueryStringValue(HKLM, Key, 'InstallLocation', ExistingInstallDir);
    Result := True;
    Exit;
  end;

  // Also recognize an unpacked/older install at the normal installer location.
  DefaultPath := ExpandConstant('{localappdata}\Programs\Chat Nexus');
  if FileExists(AddBackslash(DefaultPath) + '{#AppExeName}') then
  begin
    ExistingVersion := 'unknown / portable build';
    ExistingInstallDir := DefaultPath;
    Result := True;
  end;
end;

function InitializeSetup(): Boolean;
var
  Prompt: String;
begin
  UpgradeDetected := DetectExistingInstall();
  Result := True;

  if UpgradeDetected and (not WizardSilent()) then
  begin
    Prompt :=
      'Chat Nexus is already installed.' + #13#10 + #13#10 +
      'Installed version: ' + ExistingVersion + #13#10 +
      'New version: {#AppVersion}' + #13#10;

    if ExistingInstallDir <> '' then
      Prompt := Prompt + 'Location: ' + ExistingInstallDir + #13#10;

    Prompt := Prompt + #13#10 +
      'Upgrade now?' + #13#10 + #13#10 +
      'Your downloaded models, config.json, task/data files, and Source workspace will be preserved.';

    Result := MsgBox(Prompt, mbConfirmation, MB_YESNO) = IDYES;
  end;
end;

procedure InitializeWizard();
var
  MessageText: String;
begin
  if UpgradeDetected then
  begin
    WizardForm.Caption := 'Upgrade Chat Nexus';
    WizardForm.WelcomeLabel1.Caption := 'Upgrade Chat Nexus';

    MessageText :=
      'Setup detected an existing Chat Nexus installation.' + #13#10 + #13#10 +
      'Existing version: ' + ExistingVersion + #13#10 +
      'Installing version: {#AppVersion}' + #13#10 + #13#10 +
      'The application and bundled runtime will be updated in place.' + #13#10 +
      'Models, config.json, task/data files, and your Source Git workspace are preserved.';

    UpgradeInfoPage := CreateOutputMsgPage(
      wpWelcome,
      'Upgrade detected',
      'Your existing Chat Nexus installation will be upgraded.',
      MessageText
    );
  end;
end;

function ShouldInstallBundledSource(): Boolean;
begin
  Result := InstallBundledSource;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  InstallBundledSource := not FileExists(ExpandConstant('{app}\Source\.git\HEAD'));

  if InstallBundledSource then
    Log('No existing Source Git workspace detected; installing the bundled workspace.')
  else
    Log('Existing Source Git workspace detected; preserving it unchanged during upgrade.');

  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExampleConfig: String;
  UserConfig: String;
begin
  if CurStep = ssPostInstall then
  begin
    ExampleConfig := ExpandConstant('{app}\config.example.json');
    UserConfig := ExpandConstant('{app}\config.json');

    // config.json is created by installer code rather than [Files], so upgrades and
    // uninstall bookkeeping never overwrite/delete the user's customized config.
    if (not FileExists(UserConfig)) and FileExists(ExampleConfig) then
      FileCopy(ExampleConfig, UserConfig, False);
  end;
end;
