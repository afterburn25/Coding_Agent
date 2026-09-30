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

#define Qwen14CatalogId "qwen3-14b-q4-k-m"
#define Qwen14FileName "Qwen3-14B-Q4_K_M.gguf"
#define Qwen14Url "https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf"
#define Qwen14Sha256 "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0"
#define Qwen14Size 9001752960
#define Qwen14SourceRepo "Qwen/Qwen3-14B-GGUF"

#define Qwen30CatalogId "qwen3-coder-30b-a3b-q4-k-m"
#define Qwen30FileName "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
#define Qwen30Url "https://huggingface.co/lm-kit/qwen3-coder-30b-a3b-instruct-gguf/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
#define Qwen30Sha256 "956682fa9d36d4d0e5a80eb90ff8a001f2c48f988a497e565ae4d0c42af4fe44"
#define Qwen30Size 18556688384
#define Qwen30SourceRepo "lm-kit/qwen3-coder-30b-a3b-instruct-gguf"

[Setup]
AppId={#StableAppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Chat Nexus
DefaultGroupName=Chat Nexus
DisableProgramGroupPage=yes
DisableStartupPrompt=yes
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
CloseApplications=no
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
Source: "..\dist\ChatNexus\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "Source\*,models\*,data\*,workflows\*,config.json"

; Seed/update default workflows without replacing workflows imported or edited by the user.
Source: "..\dist\ChatNexus\workflows\*"; DestDir: "{app}\workflows"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist

; Seed the self-development workspace only when it does not already exist.
; Existing Source/.git plus local edits are preserved during updates.
Source: "..\dist\ChatNexus\Source\*"; DestDir: "{app}\Source"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: ShouldInstallBundledSource
Source: "..\dist\ChatNexus\Source\.git\*"; DestDir: "{app}\Source\.git"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: ShouldInstallBundledSource

; Coding models are downloaded by Setup directly into the final model directory.
; Inno Setup shows download/install progress, verifies SHA-256 before the final
; filename is committed, and the Check functions skip already-trusted models.
Source: "{#Qwen14Url}"; DestDir: "{app}\models"; DestName: "{#Qwen14FileName}"; ExternalSize: {#Qwen14Size}; Hash: "{#Qwen14Sha256}"; Flags: external download ignoreversion nocompression; Check: ShouldDownloadQwen14
Source: "{#Qwen30Url}"; DestDir: "{app}\models"; DestName: "{#Qwen30FileName}"; ExternalSize: {#Qwen30Size}; Hash: "{#Qwen30Sha256}"; Flags: external download ignoreversion nocompression; Check: ShouldDownloadQwen30

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
  SkipModelDownloads: Boolean;
  ModelProgressLabel: TNewStaticText;
  ModelProgressBar: TNewProgressBar;
  ModelBytesLabel: TNewStaticText;
  ModelProgressActive: Boolean;
  CurrentModelProgressNumber: Integer;
  LastModelBytesDone: Int64;

procedure InitializeModelProgressControls();
begin
  ModelProgressLabel := TNewStaticText.Create(WizardForm);
  ModelProgressLabel.Parent := WizardForm.InstallingPage;
  ModelProgressLabel.Left := WizardForm.ProgressGauge.Left;
  ModelProgressLabel.Top :=
    WizardForm.ProgressGauge.Top + WizardForm.ProgressGauge.Height + ScaleY(14);
  ModelProgressLabel.Width := WizardForm.ProgressGauge.Width;
  ModelProgressLabel.Caption := 'Coding model download';
  ModelProgressLabel.Visible := False;

  ModelProgressBar := TNewProgressBar.Create(WizardForm);
  ModelProgressBar.Parent := WizardForm.InstallingPage;
  ModelProgressBar.Left := WizardForm.ProgressGauge.Left;
  ModelProgressBar.Top :=
    ModelProgressLabel.Top + ModelProgressLabel.Height + ScaleY(4);
  ModelProgressBar.Width := WizardForm.ProgressGauge.Width;
  ModelProgressBar.Height := WizardForm.ProgressGauge.Height;
  ModelProgressBar.Min := 0;
  ModelProgressBar.Max := 1000;
  ModelProgressBar.Position := 0;
  ModelProgressBar.Visible := False;

  ModelBytesLabel := TNewStaticText.Create(WizardForm);
  ModelBytesLabel.Parent := WizardForm.InstallingPage;
  ModelBytesLabel.Left := WizardForm.ProgressGauge.Left;
  ModelBytesLabel.Top :=
    ModelProgressBar.Top + ModelProgressBar.Height + ScaleY(4);
  ModelBytesLabel.Width := WizardForm.ProgressGauge.Width;
  ModelBytesLabel.Caption := '';
  ModelBytesLabel.Visible := False;

  ModelProgressActive := False;
  CurrentModelProgressNumber := 0;
  LastModelBytesDone := 0;
end;

function LargestModelTemporaryFileSize(): Int64;
var
  FindRec: TFindRec;
  Candidate: String;
  Size: Int64;
begin
  Result := 0;
  if FindFirst(ExpandConstant('{app}\models\*.tmp'), FindRec) then
  begin
    try
      repeat
        Candidate := ExpandConstant('{app}\models\') + FindRec.Name;
        if FileSize64(Candidate, Size) and (Size > Result) then
          Result := Size;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure ShowModelDownloadProgress(
  const DisplayName, FileName: String;
  const ModelNumber: Integer;
  const ExpectedSize: Int64);
var
  BytesDone: Int64;
  Position: Integer;
begin
  if CurrentModelProgressNumber <> ModelNumber then
  begin
    CurrentModelProgressNumber := ModelNumber;
    LastModelBytesDone := 0;
    ModelProgressBar.Position := 0;
  end;

  ModelProgressActive := True;
  ModelProgressLabel.Visible := True;
  ModelProgressBar.Visible := True;
  ModelBytesLabel.Visible := True;

  ModelProgressLabel.Caption :=
    'Downloading coding model ' + IntToStr(ModelNumber) + ' of 2 - ' + DisplayName;

  BytesDone := LargestModelTemporaryFileSize();
  if BytesDone < LastModelBytesDone then
    BytesDone := LastModelBytesDone;
  if BytesDone < 0 then
    BytesDone := 0;
  if BytesDone > ExpectedSize then
    BytesDone := ExpectedSize;
  LastModelBytesDone := BytesDone;

  if ExpectedSize > 0 then
    Position := (BytesDone * 1000) div ExpectedSize
  else
    Position := 0;

  ModelProgressBar.Position := Position;
  ModelBytesLabel.Caption :=
    IntToStr(BytesDone div 1048576) + ' MB / ' +
    IntToStr(ExpectedSize div 1048576) + ' MB';
end;

procedure MarkModelDownloadsComplete();
begin
  if not ModelProgressActive then
    Exit;

  ModelProgressLabel.Caption := 'Coding model downloads complete';
  ModelProgressBar.Position := ModelProgressBar.Max;
  ModelBytesLabel.Caption := '2 of 2 default coding models ready';
  LastModelBytesDone := 0;
  CurrentModelProgressNumber := 0;
end;

function CatalogMetadataPath(const CatalogId: String): String;
begin
  Result := ExpandConstant('{app}\models\.catalog\') + CatalogId + '.json';
end;

procedure WriteCatalogMetadata(
  const CatalogId, FileName, ExpectedHash: String;
  const ExpectedSize: Int64;
  const SourceRepo: String);
var
  MetadataDir: String;
  MetadataFile: String;
  Data: AnsiString;
begin
  MetadataDir := ExpandConstant('{app}\models\.catalog');
  ForceDirectories(MetadataDir);
  MetadataFile := CatalogMetadataPath(CatalogId);

  Data :=
    '{' + #13#10 +
    '  "catalog_id": "' + CatalogId + '",' + #13#10 +
    '  "filename": "' + FileName + '",' + #13#10 +
    '  "sha256": "' + ExpectedHash + '",' + #13#10 +
    '  "size_bytes": ' + IntToStr(ExpectedSize) + ',' + #13#10 +
    '  "verified_at": 0,' + #13#10 +
    '  "source_repo": "' + SourceRepo + '"' + #13#10 +
    '}' + #13#10;

  if not SaveStringToFile(MetadataFile, Data, False) then
    Log('Warning: could not write model catalog metadata: ' + MetadataFile);
end;

function CatalogMetadataMatches(
  const CatalogId, ExpectedHash: String): Boolean;
var
  Data: AnsiString;
begin
  Result := False;
  if LoadStringFromFile(CatalogMetadataPath(CatalogId), Data) then
    Result := Pos(ExpectedHash, Data) > 0;
end;

function ModelIsInstalledAndTrusted(
  const FileName, CatalogId, ExpectedHash: String;
  const ExpectedSize: Int64;
  const SourceRepo: String): Boolean;
var
  Target: String;
  Size: Int64;
  ActualHash: String;
begin
  Result := False;
  Target := ExpandConstant('{app}\models\') + FileName;

  if not FileExists(Target) then
    Exit;

  if (not FileSize64(Target, Size)) or (Size <> ExpectedSize) then
  begin
    Log('Existing model has unexpected size and will be replaced: ' + Target);
    Exit;
  end;

  if CatalogMetadataMatches(CatalogId, ExpectedHash) then
  begin
    Log('Verified model metadata found; preserving existing model: ' + Target);
    Result := True;
    Exit;
  end;

  try
    Log('Existing model has no trusted metadata; verifying SHA-256: ' + Target);
    ActualHash := GetSHA256OfFile(Target);
    if CompareText(ActualHash, ExpectedHash) = 0 then
    begin
      WriteCatalogMetadata(CatalogId, FileName, ExpectedHash, ExpectedSize, SourceRepo);
      Result := True;
      Log('Existing model SHA-256 verified; download not required: ' + Target);
    end
    else
      Log('Existing model SHA-256 mismatch; installer will replace it: ' + Target);
  except
    Log('Could not verify existing model; installer will replace it: ' +
      Target + ' (' + GetExceptionMessage + ')');
  end;
end;

function ShouldDownloadQwen14(): Boolean;
begin
  if SkipModelDownloads then
  begin
    Result := False;
    Exit;
  end;

  Result := not ModelIsInstalledAndTrusted(
    '{#Qwen14FileName}',
    '{#Qwen14CatalogId}',
    '{#Qwen14Sha256}',
    {#Qwen14Size},
    '{#Qwen14SourceRepo}');
end;

function ShouldDownloadQwen30(): Boolean;
begin
  if SkipModelDownloads then
  begin
    Result := False;
    Exit;
  end;

  Result := not ModelIsInstalledAndTrusted(
    '{#Qwen30FileName}',
    '{#Qwen30CatalogId}',
    '{#Qwen30Sha256}',
    {#Qwen30Size},
    '{#Qwen30SourceRepo}');
end;

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
  SkipModelDownloads := CompareText(GetEnv('CHAT_NEXUS_SKIP_MODEL_DOWNLOADS'), '1') = 0;
  UpgradeDetected := DetectExistingInstall();
  Result := True;

  if SkipModelDownloads then
    Log('CHAT_NEXUS_SKIP_MODEL_DOWNLOADS=1; installer model downloads are disabled for this run.');

  if UpgradeDetected and (not WizardSilent()) then
  begin
    Prompt :=
      'Chat Nexus is already installed.' + #13#10 + #13#10 +
      'Installed version: ' + ExistingVersion + #13#10 +
      'New version: {#AppVersion}' + #13#10;

    if ExistingInstallDir <> '' then
      Prompt := Prompt + 'Location: ' + ExistingInstallDir + #13#10;

    Prompt := Prompt + #13#10 +
      'Update now?' + #13#10 + #13#10 +
      'Your downloaded models, config.json, task/data files, and Source workspace will be preserved.';

    Result := MsgBox(Prompt, mbConfirmation, MB_YESNO) = IDYES;
  end;
end;

procedure InitializeWizard();
var
  MessageText: String;
begin
  InitializeModelProgressControls();

  if UpgradeDetected then
  begin
    WizardForm.Caption := 'Update Chat Nexus';
    WizardForm.WelcomeLabel1.Caption := 'Update Chat Nexus';

    MessageText :=
      'Setup detected an existing Chat Nexus installation.' + #13#10 + #13#10 +
      'Existing version: ' + ExistingVersion + #13#10 +
      'Installing version: {#AppVersion}' + #13#10 + #13#10 +
      'The application and bundled runtime will be updated in place.' + #13#10 +
      'Models, config.json, task/data files, and your Source Git workspace are preserved.';

    UpgradeInfoPage := CreateOutputMsgPage(
      wpWelcome,
      'Update detected',
      'Your existing Chat Nexus installation will be updated.',
      MessageText
    );
  end;
end;

function ShouldInstallBundledSource(): Boolean;
begin
  Result := InstallBundledSource;
end;

procedure TaskKillImage(const ImageName: String; const Force: Boolean);
var
  ResultCode: Integer;
  Params: String;
begin
  Params := '/IM "' + ImageName + '" /T';
  if Force then
    Params := '/F ' + Params;

  if Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    Params,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode) then
  begin
    if ResultCode = 0 then
      Log('Stopped process tree: ' + ImageName)
    else
      Log('taskkill returned ' + IntToStr(ResultCode) + ' for ' + ImageName);
  end
  else
    Log('Could not execute taskkill for ' + ImageName);
end;

procedure StopRunningChatNexus();
begin
  if not UpgradeDetected then
    Exit;

  Log('Update detected; closing running Chat Nexus processes before replacing files.');

  // Avoid Restart Manager for Chat Nexus because the desktop host owns a hidden
  // backend and llama.cpp child process. Close the desktop tree first, allow a
  // short grace period, then force-clean any orphaned children.
  TaskKillImage('{#AppExeName}', False);
  Sleep(1500);
  TaskKillImage('{#AppExeName}', True);
  TaskKillImage('ChatNexus.Backend.exe', True);
  TaskKillImage('llama-server.exe', True);
  TaskKillImage('llama.exe', True);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningChatNexus();
  InstallBundledSource := not FileExists(ExpandConstant('{app}\Source\.git\HEAD'));

  if InstallBundledSource then
    Log('No existing Source Git workspace detected; installing the bundled workspace.')
  else
    Log('Existing Source Git workspace detected; preserving it unchanged during update.');

  Result := '';
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if UpgradeDetected and (CurPageID = wpReady) then
    WizardForm.NextButton.Caption := '&Update';
end;

procedure CurInstallProgressChanged(CurProgress, MaxProgress: Integer);
var
  CurrentFile: String;
begin
  CurrentFile := WizardForm.FilenameLabel.Caption;

  if Pos('{#Qwen14FileName}', CurrentFile) > 0 then
    ShowModelDownloadProgress(
      'Qwen3 14B Q4_K_M',
      '{#Qwen14FileName}',
      1,
      {#Qwen14Size})
  else if Pos('{#Qwen30FileName}', CurrentFile) > 0 then
    ShowModelDownloadProgress(
      'Qwen3-Coder 30B-A3B Instruct Q4_K_M',
      '{#Qwen30FileName}',
      2,
      {#Qwen30Size})
  else if ModelProgressActive and
    FileExists(ExpandConstant('{app}\models\{#Qwen14FileName}')) and
    FileExists(ExpandConstant('{app}\models\{#Qwen30FileName}')) then
    MarkModelDownloadsComplete();
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

    // Keep installer-downloaded models recognized as verified by Chat Nexus.
    if FileExists(ExpandConstant('{app}\models\{#Qwen14FileName}')) then
      WriteCatalogMetadata(
        '{#Qwen14CatalogId}',
        '{#Qwen14FileName}',
        '{#Qwen14Sha256}',
        {#Qwen14Size},
        '{#Qwen14SourceRepo}');

    if FileExists(ExpandConstant('{app}\models\{#Qwen30FileName}')) then
      WriteCatalogMetadata(
        '{#Qwen30CatalogId}',
        '{#Qwen30FileName}',
        '{#Qwen30Sha256}',
        {#Qwen30Size},
        '{#Qwen30SourceRepo}');
  end;
end;
