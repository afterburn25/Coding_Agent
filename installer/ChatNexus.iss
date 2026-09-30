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

#define ComfyVersion "v0.38.0"
#define ComfyArchiveName "ComfyUI_windows_portable_nvidia.7z"
#define ComfyUrl "https://github.com/Comfy-Org/ComfyUI/releases/download/v0.38.0/ComfyUI_windows_portable_nvidia.7z"
#define ComfySha256 "8f137eac345707fd7e42bcf8e29377415243011ca15522a86aed6c77331fbd56"
#define ComfySize 1994326521

#define QwenImageDiffFile "qwen_image_2.1_int8_convrot.safetensors"
#define QwenImageDiffUrl "https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors"
#define QwenImageDiffSha256 "cb74113cb03faecd79611b01fd7fd642f0aa60d6f0b95086abee214d75eaa57d"
#define QwenImageDiffSize 7256783064
#define QwenImageTextFile "qwen3vl_8b_int8_convrot.safetensors"
#define QwenImageTextUrl "https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors"
#define QwenImageTextSha256 "8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f"
#define QwenImageTextSize 9350798360
#define QwenImageVaeFile "qwen_image_2.1_vae_bf16.safetensors"
#define QwenImageVaeUrl "https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors"
#define QwenImageVaeSha256 "bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9"
#define QwenImageVaeSize 675509688

#define FluxDiffFile "flux-2-klein-4b.safetensors"
#define FluxDiffUrl "https://huggingface.co/Comfy-Org/flux2-klein/resolve/main/split_files/diffusion_models/flux-2-klein-4b.safetensors"
#define FluxDiffSha256 "ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343"
#define FluxDiffSize 7751105712
#define FluxTextFile "qwen_3_4b.safetensors"
#define FluxTextUrl "https://huggingface.co/Comfy-Org/flux2-klein/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors"
#define FluxTextSha256 "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a"
#define FluxTextSize 8044982048
#define FluxVaeFile "flux2-vae.safetensors"
#define FluxVaeUrl "https://huggingface.co/Comfy-Org/flux2-dev/resolve/main/split_files/vae/flux2-vae.safetensors"
#define FluxVaeSha256 "d64f3a68e1cc4f9f4e29b6e0da38a0204fe9a49f2d4053f0ec1fa1ca02f9c4b5"
#define FluxVaeSize 336213556

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
ArchiveExtraction=enhanced/nopassword
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

; Default local image runtime and model stack. These remain external downloads so
; Setup.exe stays small while a clean install finishes ready for image generation.
Source: "{#ComfyUrl}"; DestDir: "{app}"; DestName: "{#ComfyArchiveName}"; ExternalSize: {#ComfySize}; Hash: "{#ComfySha256}"; Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs nocompression; Check: ShouldDownloadComfyUI
Source: "{#QwenImageDiffUrl}"; DestDir: "{app}\models\image\qwen\diffusion_models"; DestName: "{#QwenImageDiffFile}"; ExternalSize: {#QwenImageDiffSize}; Hash: "{#QwenImageDiffSha256}"; Flags: external download ignoreversion nocompression; Check: ShouldDownloadQwenImageDiff
Source: "{#QwenImageTextUrl}"; DestDir: "{app}\models\image\qwen\text_encoders"; DestName: "{#QwenImageTextFile}"; ExternalSize: {#QwenImageTextSize}; Hash: "{#QwenImageTextSha256}"; Flags: external download ignoreversion nocompression; Check: ShouldDownloadQwenImageText
Source: "{#QwenImageVaeUrl}"; DestDir: "{app}\models\image\qwen\vae"; DestName: "{#QwenImageVaeFile}"; ExternalSize: {#QwenImageVaeSize}; Hash: "{#QwenImageVaeSha256}"; Flags: external download ignoreversion nocompression; Check: ShouldDownloadQwenImageVae
Source: "{#FluxDiffUrl}"; DestDir: "{app}\models\image\flux\diffusion_models"; DestName: "{#FluxDiffFile}"; ExternalSize: {#FluxDiffSize}; Hash: "{#FluxDiffSha256}"; Flags: external download ignoreversion nocompression; Check: ShouldDownloadFluxDiff
Source: "{#FluxTextUrl}"; DestDir: "{app}\models\image\flux\text_encoders"; DestName: "{#FluxTextFile}"; ExternalSize: {#FluxTextSize}; Hash: "{#FluxTextSha256}"; Flags: external download ignoreversion nocompression; Check: ShouldDownloadFluxText
Source: "{#FluxVaeUrl}"; DestDir: "{app}\models\image\flux\vae"; DestName: "{#FluxVaeFile}"; ExternalSize: {#FluxVaeSize}; Hash: "{#FluxVaeSha256}"; Flags: external download ignoreversion nocompression; Check: ShouldDownloadFluxVae

[Dirs]
Name: "{app}\models"
Name: "{app}\models\image\qwen\diffusion_models"
Name: "{app}\models\image\qwen\text_encoders"
Name: "{app}\models\image\qwen\vae"
Name: "{app}\models\image\flux\diffusion_models"
Name: "{app}\models\image\flux\text_encoders"
Name: "{app}\models\image\flux\vae"
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
  ModelProgressLabel.Caption := 'Bootstrap download';
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

function LargestTemporaryFileSize(const Directory: String): Int64;
var
  FindRec: TFindRec;
  Candidate: String;
  Size: Int64;
  SearchDir: String;
begin
  Result := 0;
  SearchDir := AddBackslash(Directory);
  if FindFirst(SearchDir + '*.tmp', FindRec) then
  begin
    try
      repeat
        Candidate := SearchDir + FindRec.Name;
        if FileSize64(Candidate, Size) and (Size > Result) then
          Result := Size;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure ShowModelDownloadProgress(
  const DisplayName, DownloadDirectory: String;
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
    'Downloading component ' + IntToStr(ModelNumber) + ' of 9 - ' + DisplayName;

  BytesDone := LargestTemporaryFileSize(DownloadDirectory);
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

  ModelProgressLabel.Caption := 'Bootstrap downloads complete';
  ModelProgressBar.Position := ModelProgressBar.Max;
  ModelBytesLabel.Caption := '9 of 9 default runtime/model components ready';
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

function BootstrapFilePresent(const RelativePath: String; const ExpectedSize: Int64): Boolean;
var
  Target: String;
  Size: Int64;
begin
  Target := ExpandConstant('{app}\') + RelativePath;
  Result := FileExists(Target) and FileSize64(Target, Size) and (Size = ExpectedSize);
  if Result then
    Log('Bootstrap component already present with expected size: ' + Target);
end;

function ShouldDownloadComfyUI(): Boolean;
var
  Marker: String;
  VersionText: AnsiString;
begin
  if SkipModelDownloads then
  begin
    Result := False;
    Exit;
  end;

  Marker := ExpandConstant('{app}\ComfyUI_windows_portable\.chatnexus-version');
  if FileExists(ExpandConstant('{app}\ComfyUI_windows_portable\ComfyUI\main.py')) and
     FileExists(ExpandConstant('{app}\ComfyUI_windows_portable\python_embeded\python.exe')) and
     LoadStringFromFile(Marker, VersionText) and
     (Pos('{#ComfyVersion}', VersionText) > 0) then
    Result := False
  else
    Result := True;
end;

function ShouldDownloadQwenImageDiff(): Boolean;
begin
  Result := (not SkipModelDownloads) and
    (not BootstrapFilePresent('models\image\qwen\diffusion_models\{#QwenImageDiffFile}', {#QwenImageDiffSize}));
end;

function ShouldDownloadQwenImageText(): Boolean;
begin
  Result := (not SkipModelDownloads) and
    (not BootstrapFilePresent('models\image\qwen\text_encoders\{#QwenImageTextFile}', {#QwenImageTextSize}));
end;

function ShouldDownloadQwenImageVae(): Boolean;
begin
  Result := (not SkipModelDownloads) and
    (not BootstrapFilePresent('models\image\qwen\vae\{#QwenImageVaeFile}', {#QwenImageVaeSize}));
end;

function ShouldDownloadFluxDiff(): Boolean;
begin
  Result := (not SkipModelDownloads) and
    (not BootstrapFilePresent('models\image\flux\diffusion_models\{#FluxDiffFile}', {#FluxDiffSize}));
end;

function ShouldDownloadFluxText(): Boolean;
begin
  Result := (not SkipModelDownloads) and
    (not BootstrapFilePresent('models\image\flux\text_encoders\{#FluxTextFile}', {#FluxTextSize}));
end;

function ShouldDownloadFluxVae(): Boolean;
begin
  Result := (not SkipModelDownloads) and
    (not BootstrapFilePresent('models\image\flux\vae\{#FluxVaeFile}', {#FluxVaeSize}));
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
    Log('CHAT_NEXUS_SKIP_MODEL_DOWNLOADS=1; installer bootstrap downloads are disabled for this run.');

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
    ShowModelDownloadProgress('Qwen3 14B Q4_K_M', ExpandConstant('{app}\models'), 1, {#Qwen14Size})
  else if Pos('{#Qwen30FileName}', CurrentFile) > 0 then
    ShowModelDownloadProgress('Qwen3-Coder 30B-A3B', ExpandConstant('{app}\models'), 2, {#Qwen30Size})
  else if Pos('{#ComfyArchiveName}', CurrentFile) > 0 then
    ShowModelDownloadProgress('ComfyUI {#ComfyVersion} NVIDIA runtime', ExpandConstant('{app}'), 3, {#ComfySize})
  else if Pos('{#QwenImageDiffFile}', CurrentFile) > 0 then
    ShowModelDownloadProgress('Qwen Image 2.1 diffusion model', ExpandConstant('{app}\models\image\qwen\diffusion_models'), 4, {#QwenImageDiffSize})
  else if Pos('{#QwenImageTextFile}', CurrentFile) > 0 then
    ShowModelDownloadProgress('Qwen Image 2.1 text encoder', ExpandConstant('{app}\models\image\qwen\text_encoders'), 5, {#QwenImageTextSize})
  else if Pos('{#QwenImageVaeFile}', CurrentFile) > 0 then
    ShowModelDownloadProgress('Qwen Image 2.1 VAE', ExpandConstant('{app}\models\image\qwen\vae'), 6, {#QwenImageVaeSize})
  else if Pos('{#FluxDiffFile}', CurrentFile) > 0 then
    ShowModelDownloadProgress('Flux.2 Klein 4B diffusion model', ExpandConstant('{app}\models\image\flux\diffusion_models'), 7, {#FluxDiffSize})
  else if Pos('{#FluxTextFile}', CurrentFile) > 0 then
    ShowModelDownloadProgress('Flux.2 Klein text encoder', ExpandConstant('{app}\models\image\flux\text_encoders'), 8, {#FluxTextSize})
  else if Pos('{#FluxVaeFile}', CurrentFile) > 0 then
    ShowModelDownloadProgress('Flux.2 VAE', ExpandConstant('{app}\models\image\flux\vae'), 9, {#FluxVaeSize});
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

    if FileExists(ExpandConstant('{app}\ComfyUI_windows_portable\ComfyUI\main.py')) then
      SaveStringToFile(
        ExpandConstant('{app}\ComfyUI_windows_portable\.chatnexus-version'),
        '{#ComfyVersion}' + #13#10,
        False);

    MarkModelDownloadsComplete();

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
