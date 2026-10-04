#ifndef AppVersion
  #define AppVersion "0.19.0"
#endif

#ifndef AppNumericVersion
  #define AppNumericVersion "0.19.0.0"
#endif

#define AppName "Nexus Core"
#define AppPublisher "Afterburn25"
#define AppExeName "NexusCore.exe"
#define StableAppId "ChatNexus.Afterburn25"

#define Qwen4CatalogId "qwen3-4b-instruct-q4-k-m"
#define Qwen4FileName "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
#define Qwen4Url "https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
#define Qwen4Sha256 "2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e"
#define Qwen4Size 2497280736
#define Qwen4SourceRepo "bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF"

#define Qwen8CatalogId "qwen3-8b-q4-k-m"
#define Qwen8FileName "Qwen3-8B-Q4_K_M.gguf"
#define Qwen8Url "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf"
#define Qwen8Sha256 "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
#define Qwen8Size 5027783488
#define Qwen8SourceRepo "Qwen/Qwen3-8B-GGUF"

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

; Kokoro-82M voice assets (Apache-2.0, hexgrad/Kokoro-82M, ONNX build
; distributed with kokoro-onnx 0.6.1). Hashes verified against the files
; Nexus Core actually synthesized with.
#define KokoroModelUrl "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
#define KokoroModelSha256 "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
#define KokoroModelSize 325532387
#define KokoroVoicesUrl "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
#define KokoroVoicesSha256 "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
#define KokoroVoicesSize 28214398

; Optional tools (ComfyUI, image model packs, etc.) are intentionally NOT
; downloaded by Setup — they install on demand from the in-app Tools page.

[Setup]
AppId={#StableAppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={code:PreferredInstallDir}
DefaultGroupName=Nexus Core
DisableProgramGroupPage=yes
DisableStartupPrompt=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UsePreviousAppDir=yes
UsePreviousGroup=yes
; DefaultDirName already resolves to <drive>:\Nexus_Core — appending the
; app name again would recreate the nested Nexus_Core\Nexus_Core bug.
AppendDefaultDirName=no
DirExistsWarning=no
OutputDir=..\dist\installer
OutputBaseFilename=NexusCore-Setup-{#AppVersion}-Windows-x64
SetupIconFile=..\desktop\ChatNexus.Desktop\nexus-core.ico
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
VersionInfoDescription=Nexus Core Installer
VersionInfoCopyright=Nexus Core
MinVersion=10.0.17763
; Downloads stage on the install drive via a junction ({tmp}\dl ->
; {ModelsDir}\.dl) because Inno can only write downloads under {tmp}.
; RedirectionGuard would refuse to traverse that junction — disable it so
; our own redirection is legal.
RedirectionGuard=no
ChangesEnvironment=no
ChangesAssociations=no
Uninstallable=yes
#ifdef WithSign
SignTool=standard
SignedUninstaller=yes
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Replace application/runtime files on every install or upgrade, but never overwrite
; mutable user state or the bundled self-development Git workspace.
; Top-level files only, NO recursion: a bare-name Excludes pattern matches
; at ANY depth, which silently stripped nested package files before
; (kokoro_onnx\config.json, backend\_internal\tools\manifests, ...).
Source: "..\dist\ChatNexus\*"; DestDir: "{app}"; Flags: ignoreversion; Excludes: "config.json"
; Payload subtrees are all needed — recursive with no exclusions.
Source: "..\dist\ChatNexus\backend\*"; DestDir: "{app}\backend"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\ChatNexus\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

; Seed/update default workflows without replacing workflows imported or edited by the user.
Source: "..\dist\ChatNexus\workflows\*"; DestDir: "{app}\workflows"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist

; Seed the self-development workspace only when it does not already exist.
; Existing Source/.git plus local edits are preserved during updates.
Source: "..\dist\ChatNexus\Source\*"; DestDir: "{app}\Source"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: ShouldInstallBundledSource
Source: "..\dist\ChatNexus\Source\.git\*"; DestDir: "{app}\Source\.git"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: ShouldInstallBundledSource

; Coding models and the Kokoro voice assets are fetched by TDownloadWizardPage
; when the user clicks Install — Inno's built-in download UI with a live
; per-file bar, its own message pump, and a working Abort button ([Files]
; `download` and synchronous PrepareToInstall calls can't do any of that).
; Files land in {tmp} and StageVerifiedDownloads hash-checks and copies them
; into the models dir on every download-page exit (success, abort, failure),
; so completed downloads survive aborts and successful runs don't double-copy
; ~35 GB. Smallest to largest: 4B, 8B, 14B, 30B hybrid, then voice assets.

; Optional tools (ComfyUI portable, image model packs) are downloaded by the
; in-app Tools page instead of Setup, keeping installation fast.

[Dirs]
Name: "{code:ModelsDir}"
Name: "{app}\data"

[InstallDelete]
; Clean the obsolete pywebview portable runtime from early dogfood builds if an
; installer is pointed at that same directory. Mutable model/data/source paths are untouched.
Type: filesandordirs; Name: "{app}\_internal"
; Remove the legacy Chat Nexus desktop exe on upgrades; Nexus Core ships NexusCore.exe.
Type: files; Name: "{app}\ChatNexus.exe"

[Icons]
Name: "{autoprograms}\Nexus Core"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\Nexus Core"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Launch through cmd so the app never inherits setup's environment or
; open handles: __COMPAT_LAYER (a PCA shim on this unsigned installer
; propagates to children and wedges the single-file apphost pre-runtime)
; is scrubbed, and a short settle lets AV/release scanning finish before
; the 190MB exe first maps itself.
Filename: "{cmd}"; Parameters: "/c timeout /t 3 /nobreak >nul & set ""__COMPAT_LAYER="" & start """" /D ""{app}"" ""{app}\{#AppExeName}"""; Description: "Launch Nexus Core"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent runhidden

[Code]
var
  UpgradeDetected: Boolean;
  ExistingVersion: String;
  ExistingInstallDir: String;
  UpgradeInfoPage: TOutputMsgWizardPage;
  UninstallButton: TNewButton;
  InstallBundledSource: Boolean;
  SkipModelDownloads: Boolean;
  ModelPlanPage: TOutputMsgWizardPage;
  RamGB, VramGB, CoreCount: Integer;
  GpuDesc, CpuDesc: String;
  RecQwen4, RecQwen8, RecQwen14, RecQwen30: Boolean;
  DownloadTotalLabel: TNewStaticText;
  DownloadTotalBar: TNewProgressBar;
  InstallFilesWritten: Boolean;
  InstallCompleted: Boolean;
  DownloadPage: TDownloadWizardPage;
  DoQwen4, DoQwen8, DoQwen14, DoQwen30, DoKokoroModel, DoKokoroVoices: Boolean;
  DlTotalBytes, DlDoneBytes: Int64;
  DlLastFile: String;
  DlLastCounted: Boolean;
  DlCompleted: TStringList;
  DlLastCompleted: Boolean;
  DlStagingRedirected: Boolean;
  DlForceExit: Boolean;

function GetDriveType(lpRootPathName: String): UINT;
  external 'GetDriveTypeW@kernel32.dll stdcall';

function GetFileAttributesW(lpFileName: String): DWORD;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function GetPhysicallyInstalledSystemMemory(var TotalMemoryInKilobytes: Int64): BOOL;
  external 'GetPhysicallyInstalledSystemMemory@kernel32.dll stdcall';

// Models live in {app}\models like everything else. Older installs had
// {app}\models junctioned to <drive>:\NexusCore\models — only while the
// junction itself still exists do downloads/catalog writes go to the
// real directory (junctions are untrusted mount points for creation);
// the host's RehomeDriveStateDirs moves them into {app}\models on first
// launch. A bare leftover Target dir must NOT redirect — the existence
// check recreated the legacy root and split the layout again.
function ModelsDir(Param: String): String;
var
  Link: String;
  Target: String;
  Attr: DWORD;
begin
  Link := ExpandConstant('{app}\models');
  Target := ExtractFileDrive(ExpandConstant('{app}')) + '\NexusCore\models';
  Attr := GetFileAttributesW(Link);
  if (Attr <> $FFFFFFFF) and ((Attr and $400) <> 0) then
    Result := Target
  else
    Result := Link;
end;

function PreferredInstallDir(Param: String): String;
var
  Letter: Integer;
  Root: String;
  BestRoot: String;
  FreeBytes: Int64;
  TotalBytes: Int64;
  BestFree: Int64;
begin
  // Flat layout: install directly at <drive>:\Nexus_Core.
  // Default to C:\; when additional fixed drives exist, prefer whichever
  // fixed drive has the most free space.
  BestRoot := 'C:\';
  BestFree := -1;
  for Letter := Ord('C') to Ord('Z') do
  begin
    Root := Chr(Letter) + ':\';
    if (GetDriveType(Root) = 3) and GetSpaceOnDisk64(Root, FreeBytes, TotalBytes) then
    begin
      if FreeBytes > BestFree then
      begin
        BestFree := FreeBytes;
        BestRoot := Root;
      end;
    end;
  end;
  Result := BestRoot + 'Nexus_Core';
end;

procedure UninstallButtonClick(Sender: TObject);
var
  UninstPath: String;
  ResultCode: Integer;
  WaitCount: Integer;
begin
  if ExistingInstallDir = '' then
    Exit;

  UninstPath := AddBackslash(ExistingInstallDir) + 'unins000.exe';
  if not FileExists(UninstPath) then
  begin
    MsgBox(
      'Could not find the existing Nexus Core uninstaller at:' + #13#10 + UninstPath,
      mbError, MB_OK);
    Exit;
  end;

  if MsgBox(
    'Uninstall the existing Nexus Core installation at ' + ExistingInstallDir + '?' + #13#10 + #13#10 +
    'The uninstaller will run first; this setup will then continue as a fresh installation.',
    mbConfirmation, MB_YESNO) <> IDYES then
    Exit;

  UninstallButton.Enabled := False;
  UninstallButton.Caption := 'Uninstalling...';

  if Exec(UninstPath, '', ExistingInstallDir, SW_SHOW, ewWaitUntilTerminated, ResultCode) then
  begin
    // unins000.exe copies itself to a temp dir and returns before the real
    // uninstall finishes; wait until the original unins000.exe is removed.
    WaitCount := 0;
    while FileExists(UninstPath) and (WaitCount < 600) do
    begin
      Sleep(500);
      WaitCount := WaitCount + 1;
      UninstallButton.Caption :=
        'Uninstalling... ' + IntToStr(WaitCount div 2) + 's';
      WizardForm.Update;
    end;
  end;

  if FileExists(UninstPath) then
  begin
    UninstallButton.Enabled := True;
    UninstallButton.Caption := 'Uninstall existing version';
    Exit;
  end;

  UpgradeDetected := False;
  UninstallButton.Visible := False;
  WizardForm.DirEdit.Text := PreferredInstallDir('');
  MsgBox(
    'The previous Nexus Core installation was removed.' + #13#10 +
    'Setup will continue with a fresh installation.',
    mbInformation, MB_OK);
end;

// ---------------------------------------------------------------------------
// Model downloads — driven by TDownloadWizardPage, Inno's built-in download
// UI. It runs its own message pump, so the wizard stays responsive, shows a
// live per-file progress bar, and has a working Abort button. [Files]
// `external download` cannot do any of that from script: it exposes no
// progress callback (CurInstallProgressChanged fires once per file) and
// running DownloadTemporaryFile synchronously in PrepareToInstall froze the
// whole window. Files land in {tmp} and [Files] copies them into the models
// dir during install.
// ---------------------------------------------------------------------------

function ModelDlProgress(
  const Url, FileName: String;
  const Progress, ProgressMax: Int64): Boolean;
var
  TotalSoFar: Int64;
begin
  Result := True;

  // The page's own bar shows this file's bytes live; our extra bar shows
  // the running total across the whole queue.
  if (not WizardSilent()) and (DlTotalBytes > 0) then
  begin
    TotalSoFar := DlDoneBytes + Progress;
    DownloadTotalBar.Position := (TotalSoFar * 1000) div DlTotalBytes;
    DownloadTotalLabel.Caption :=
      'Total: ' + IntToStr(TotalSoFar div 1048576) + ' / ' +
      IntToStr(DlTotalBytes div 1048576) + ' MB';
  end;

  if FileName <> DlLastFile then
  begin
    DlLastFile := FileName;
    DlLastCounted := False;
    DlLastCompleted := False;
  end;
  if (Progress = ProgressMax) and (ProgressMax > 0) and (not DlLastCounted) then
  begin
    DlLastCounted := True;
    DlDoneBytes := DlDoneBytes + ProgressMax;
    Log('Download complete: ' + FileName + ' (' + IntToStr(Progress) + ' bytes)');
  end;
  if (Progress = ProgressMax) and (ProgressMax > 0) and (not DlLastCompleted) then
  begin
    DlLastCompleted := True;
    // Queued basenames carry the 'dl\' staging-dir prefix — store the leaf
    // name so hash lookups and catalog writes match.
    if DlCompleted <> nil then
      DlCompleted.Add(ExtractFileName(FileName));
    // Reassert the total in case Inno repaints between files — the label is
    // ours, so keep it alive across the queue boundary.
    if (not WizardSilent()) and (DlTotalBytes > 0) then
      DownloadTotalLabel.Caption :=
        'Total: ' + IntToStr(DlDoneBytes div 1048576) + ' / ' +
        IntToStr(DlTotalBytes div 1048576) + ' MB — ' + FileName + ' done';
  end;
end;

procedure InitializeDownloadPage();
begin
  // Inno's built-in download page: it pumps its own messages, so the wizard
  // stays movable, its per-file bar streams live bytes, and Abort actually
  // works — none of which synchronous script downloads can do. We add one
  // extra bar tracking total bytes across every queued model so the page
  // shows per-file AND overall progress.
  DownloadPage := CreateDownloadPage(
    'Downloading models',
    'Fetching the models selected for this hardware. Models already on ' +
      'disk and verified are skipped.',
    @ModelDlProgress);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
  DlCompleted := TStringList.Create;

  DownloadTotalLabel := TNewStaticText.Create(DownloadPage);
  DownloadTotalLabel.Parent := DownloadPage.Surface;
  DownloadTotalLabel.Left := DownloadPage.ProgressBar.Left;
  DownloadTotalLabel.Width := DownloadPage.ProgressBar.Width;
  DownloadTotalLabel.Top := DownloadPage.SurfaceHeight - ScaleY(52);
  DownloadTotalLabel.Caption := 'Total download';

  DownloadTotalBar := TNewProgressBar.Create(DownloadPage);
  DownloadTotalBar.Parent := DownloadPage.Surface;
  DownloadTotalBar.Left := DownloadPage.ProgressBar.Left;
  DownloadTotalBar.Top :=
    DownloadTotalLabel.Top + DownloadTotalLabel.Height + ScaleY(4);
  DownloadTotalBar.Width := DownloadPage.ProgressBar.Width;
  DownloadTotalBar.Height := DownloadPage.ProgressBar.Height;
  DownloadTotalBar.Min := 0;
  DownloadTotalBar.Max := 1000;
  DownloadTotalBar.Position := 0;
end;

// Long operations (process shutdown, SHA-256 of multi-GB models) otherwise run
// with a completely static wizard, which reads as "frozen". Push an explicit
// stage message onto whichever page is currently visible.
procedure ShowBusyStatus(const Primary, Detail: String);
begin
  if WizardSilent() then
    Exit;

  if WizardForm.CurPageID = wpReady then
    WizardForm.ReadyLabel.Caption :=
      Primary + #13#10 + #13#10 + Detail
  else
  begin
    WizardForm.StatusLabel.Caption := Primary;
    WizardForm.FilenameLabel.Caption := Detail;
  end;

  WizardForm.Update;
end;

// Sleep() alone freezes the wizard repaint; chunk it so status text stays live.
procedure BusySleep(Milliseconds: Integer);
begin
  while Milliseconds > 0 do
  begin
    if Milliseconds > 200 then
      Sleep(200)
    else
      Sleep(Milliseconds);
    Milliseconds := Milliseconds - 200;
    if not WizardSilent() then
      WizardForm.Update;
  end;
end;

function InstalledUninstallKey(): String;
begin
  Result := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#StableAppId}_is1';
end;

function IsPreservedPayload(const Name: String): Boolean;
// Payload that is never "install progress": user state, downloaded
// models, tools, the Source workspace, and user workflows/config.
begin
  Result := (CompareText(Name, 'models') = 0) or
            (CompareText(Name, 'tools') = 0) or
            (CompareText(Name, 'data') = 0) or
            (CompareText(Name, '.agent') = 0) or
            (CompareText(Name, 'output') = 0) or
            (CompareText(Name, 'Source') = 0) or
            (CompareText(Name, 'workflows') = 0) or
            (CompareText(Name, 'config.json') = 0);
end;

procedure CleanupAbortedInstall;
// Inno's built-in rollback already removes files it tracked. What's
// left on abort is the {app} skeleton plus untracked bits — sweep them
// so a canceled install does not leave a half-install that the next
// launch (or the next setup's detection) mistakes for a real install.
// Preserved payloads (models, tools, state, Source) stay: they are not
// install progress, and they may predate this run on updates.
var
  FindRec: TFindRec;
  AppDir, ItemPath: String;
begin
  AppDir := ExpandConstant('{app}');
  if FindFirst(AppDir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') and
           not IsPreservedPayload(FindRec.Name) then
        begin
          ItemPath := AppDir + '\' + FindRec.Name;
          if (FindRec.Attributes and $10) <> 0 then
          begin
            if (FindRec.Attributes and $400) <> 0 then
              RemoveDir(ItemPath)
            else
              DelTree(ItemPath, True, True, True);
          end
          else
            DeleteFile(ItemPath);
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
  RemoveDir(AppDir);
end;

procedure DeinitializeSetup();
begin
  if InstallFilesWritten and not InstallCompleted then
  begin
    Log('Setup aborted after files were written — cleaning partial install.');
    CleanupAbortedInstall;
    // An aborted update also leaves the previous install's registry
    // entry describing a now-broken install — drop it so the next run
    // is a clean fresh install, not an "update" of a partial.
    RegDeleteKeyIncludingSubkeys(HKCU, InstalledUninstallKey());
    RegDeleteKeyIncludingSubkeys(HKLM, InstalledUninstallKey());
  end;
end;

procedure CurInstallProgressChanged(CurProgress, MaxProgress: Integer);
begin
  InstallFilesWritten := True;
end;

function CatalogMetadataPath(const CatalogId: String): String;
begin
  Result := ExpandConstant('{code:ModelsDir}\.catalog\') + CatalogId + '.json';
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
  MetadataDir := ExpandConstant('{code:ModelsDir}\.catalog');
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
  Target := ExpandConstant('{code:ModelsDir}\') + FileName;

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
    ShowBusyStatus(
      'Verifying existing model file...',
      'Computing SHA-256 of ' + FileName + ' (' +
        IntToStr((ExpectedSize + 536870911) div 1073741824) + ' GB). ' +
        'Large models can take a few minutes - Setup is still working, please do not close it.');
    if (not WizardSilent()) and (DownloadPage <> nil) then
      DownloadPage.SetText(
        'Checking existing models',
        'Computing SHA-256 of ' + FileName + ' (' +
          IntToStr((ExpectedSize + 536870911) div 1073741824) + ' GB). ' +
          'Large models can take a minute — please wait.');
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

function ShouldDownloadQwen4(): Boolean;
begin
  if SkipModelDownloads or not RecQwen4 then
  begin
    Result := False;
    Exit;
  end;

  Result := not ModelIsInstalledAndTrusted(
    '{#Qwen4FileName}',
    '{#Qwen4CatalogId}',
    '{#Qwen4Sha256}',
    {#Qwen4Size},
    '{#Qwen4SourceRepo}');
end;

function ShouldDownloadQwen8(): Boolean;
begin
  if SkipModelDownloads or not RecQwen8 then
  begin
    Result := False;
    Exit;
  end;

  Result := not ModelIsInstalledAndTrusted(
    '{#Qwen8FileName}',
    '{#Qwen8CatalogId}',
    '{#Qwen8Sha256}',
    {#Qwen8Size},
    '{#Qwen8SourceRepo}');
end;

function ShouldDownloadQwen14(): Boolean;
begin
  if SkipModelDownloads or not RecQwen14 then
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
  if SkipModelDownloads or not RecQwen30 then
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

function ShouldDownloadKokoroModel(): Boolean;
begin
  if SkipModelDownloads then
  begin
    Result := False;
    Exit;
  end;

  Result := not ModelIsInstalledAndTrusted(
    'voice\kokoro-v1.0.onnx',
    'kokoro-v1-0-onnx',
    '{#KokoroModelSha256}',
    {#KokoroModelSize},
    'hexgrad/Kokoro-82M');
end;

function ShouldDownloadKokoroVoices(): Boolean;
begin
  if SkipModelDownloads then
  begin
    Result := False;
    Exit;
  end;

  Result := not ModelIsInstalledAndTrusted(
    'voice\voices-v1.0.bin',
    'kokoro-voices-v1-0',
    '{#KokoroVoicesSha256}',
    {#KokoroVoicesSize},
    'hexgrad/Kokoro-82M');
end;

procedure EvaluateModelChecks();
// May SHA-256 multi-GB existing files — show busy text first so the wait
// reads as work, not a freeze.
begin
  DoQwen4 := ShouldDownloadQwen4();
  DoQwen8 := ShouldDownloadQwen8();
  DoQwen14 := ShouldDownloadQwen14();
  DoQwen30 := ShouldDownloadQwen30();
  DoKokoroModel := ShouldDownloadKokoroModel();
  DoKokoroVoices := ShouldDownloadKokoroVoices();
end;

function AnyModelQueued(): Boolean;
begin
  Result := DoQwen4 or DoQwen8 or DoQwen14 or DoQwen30 or
    DoKokoroModel or DoKokoroVoices;
end;

procedure QueueModelDownloads();
begin
  DownloadPage.Clear;
  DlTotalBytes := 0;
  DlDoneBytes := 0;
  DlLastFile := '';
  DlLastCounted := False;

  // Basenames carry the 'dl\' prefix so every file lands in the staging dir —
  // a junction onto the install drive (see EnsureDlStaging), where the space
  // actually is. Empty SHA-256 on purpose: Inno's per-file verify ran a
  // synchronous hash between files (25-45s on multi-GB models) during which
  // the page's text blanked and Abort could not be answered. Integrity is
  // enforced in StageVerifiedDownloads instead — only hash-matching files are
  // moved into models\ and cataloged.
  if DoQwen4 then
  begin
    DownloadPage.Add('{#Qwen4Url}', 'dl\{#Qwen4FileName}', '');
    DlTotalBytes := DlTotalBytes + {#Qwen4Size};
  end;
  if DoQwen8 then
  begin
    DownloadPage.Add('{#Qwen8Url}', 'dl\{#Qwen8FileName}', '');
    DlTotalBytes := DlTotalBytes + {#Qwen8Size};
  end;
  if DoQwen14 then
  begin
    DownloadPage.Add('{#Qwen14Url}', 'dl\{#Qwen14FileName}', '');
    DlTotalBytes := DlTotalBytes + {#Qwen14Size};
  end;
  if DoQwen30 then
  begin
    DownloadPage.Add('{#Qwen30Url}', 'dl\{#Qwen30FileName}', '');
    DlTotalBytes := DlTotalBytes + {#Qwen30Size};
  end;
  if DoKokoroModel then
  begin
    DownloadPage.Add('{#KokoroModelUrl}', 'dl\kokoro-v1.0.onnx', '');
    DlTotalBytes := DlTotalBytes + {#KokoroModelSize};
  end;
  if DoKokoroVoices then
  begin
    DownloadPage.Add('{#KokoroVoicesUrl}', 'dl\voices-v1.0.bin', '');
    DlTotalBytes := DlTotalBytes + {#KokoroVoicesSize};
  end;
end;

// Catalog hooks — stamp trust metadata the moment a verified download is
// staged into models\, not at ssPostInstall. An aborted run then still
// leaves trusted files, so the next run skips re-download AND the multi-GB
// SHA-256 re-check that froze the Ready page.
procedure CatalogQwen4();
begin
  WriteCatalogMetadata('{#Qwen4CatalogId}', '{#Qwen4FileName}',
    '{#Qwen4Sha256}', {#Qwen4Size}, '{#Qwen4SourceRepo}');
end;

procedure CatalogQwen8();
begin
  WriteCatalogMetadata('{#Qwen8CatalogId}', '{#Qwen8FileName}',
    '{#Qwen8Sha256}', {#Qwen8Size}, '{#Qwen8SourceRepo}');
end;

procedure CatalogQwen14();
begin
  WriteCatalogMetadata('{#Qwen14CatalogId}', '{#Qwen14FileName}',
    '{#Qwen14Sha256}', {#Qwen14Size}, '{#Qwen14SourceRepo}');
end;

procedure CatalogQwen30();
begin
  WriteCatalogMetadata('{#Qwen30CatalogId}', '{#Qwen30FileName}',
    '{#Qwen30Sha256}', {#Qwen30Size}, '{#Qwen30SourceRepo}');
end;

procedure CatalogKokoroModel();
begin
  WriteCatalogMetadata('kokoro-v1-0-onnx', 'kokoro-v1.0.onnx',
    '{#KokoroModelSha256}', {#KokoroModelSize}, 'hexgrad/Kokoro-82M');
end;

procedure CatalogKokoroVoices();
begin
  WriteCatalogMetadata('kokoro-voices-v1-0', 'voices-v1.0.bin',
    '{#KokoroVoicesSha256}', {#KokoroVoicesSize}, 'hexgrad/Kokoro-82M');
end;

function ExpectedHashFor(const FileName: String): String;
begin
  if FileName = '{#Qwen4FileName}' then Result := '{#Qwen4Sha256}'
  else if FileName = '{#Qwen8FileName}' then Result := '{#Qwen8Sha256}'
  else if FileName = '{#Qwen14FileName}' then Result := '{#Qwen14Sha256}'
  else if FileName = '{#Qwen30FileName}' then Result := '{#Qwen30Sha256}'
  else if FileName = 'kokoro-v1.0.onnx' then Result := '{#KokoroModelSha256}'
  else if FileName = 'voices-v1.0.bin' then Result := '{#KokoroVoicesSha256}'
  else Result := '';
end;

function ExpectedSizeFor(const FileName: String): Int64;
begin
  if FileName = '{#Qwen4FileName}' then Result := {#Qwen4Size}
  else if FileName = '{#Qwen8FileName}' then Result := {#Qwen8Size}
  else if FileName = '{#Qwen14FileName}' then Result := {#Qwen14Size}
  else if FileName = '{#Qwen30FileName}' then Result := {#Qwen30Size}
  else if FileName = 'kokoro-v1.0.onnx' then Result := {#KokoroModelSize}
  else if FileName = 'voices-v1.0.bin' then Result := {#KokoroVoicesSize}
  else Result := -1;
end;

function DlStagingDir(): String;
// Logical path Inno writes downloads into. Physically it is a junction into
// {code:ModelsDir}\.dl when EnsureDlStaging succeeded — bytes then land on
// the install drive (which had the free space), not the system drive.
begin
  Result := ExpandConstant('{tmp}\dl');
end;

procedure EnsureDlStaging();
var
  Link, Target: String;
  ResultCode: Integer;
begin
  DlStagingRedirected := False;
  Target := ExpandConstant('{code:ModelsDir}\.dl');
  ForceDirectories(Target);
  Link := DlStagingDir();
  // RedirectionGuard is off so our junction can be traversed — which also
  // means a pre-planted dl junction would be honored. Never trust whatever
  // is already there: delete it and always link to our own target.
  if DirExists(Link) or FileExists(Link) then
  begin
    if (GetFileAttributesW(Link) and $400) <> 0 then
      RemoveDir(Link)              // junction/symlink: unlink only, never traverse
    else
      DelTree(Link, True, True, True);
  end;
  Exec(ExpandConstant('{cmd}'),
    '/c mklink /J "' + Link + '" "' + Target + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if DirExists(Link) then
  begin
    DlStagingRedirected := True;
    Log('Download staging junctioned to install drive: ' + Link + ' -> ' + Target);
  end
  else
  begin
    // mklink failed (non-NTFS target, locked dir, ...) — real dir in {tmp}.
    ForceDirectories(Link);
    Log('Download staging junction failed; downloads stay under ' + Link);
  end;
end;

procedure MoveVerifiedDownload(const FileName: String; const DeepVerify: Boolean);
// Move a completed file from the staging dir into models\ and stamp catalog
// metadata. DeepVerify=1 runs a full SHA-256 — reserved for .dl leftovers of
// unknown provenance (crashed/aborted prior runs). Files Inno just reported
// complete get an instant size check instead: SHA-256 of an 18 GB model is a
// ~90-second synchronous block that froze the page between "downloaded" and
// "installed". RenameFile is instant when staging sits on the install drive
// (junction); CopyFile is the cross-drive fallback.
var
  SrcPath, DestPath, SubDir: String;
  ActualSize: Int64;
begin
  SrcPath := DlStagingDir() + '\' + FileName;
  if not FileExists(SrcPath) then
    Exit;
  if DeepVerify then
  begin
    if (ExpectedHashFor(FileName) <> '') and
       (GetSHA256OfFile(SrcPath) <> ExpectedHashFor(FileName)) then
    begin
      Log('Hash mismatch — discarding download: ' + SrcPath);
      DeleteFile(SrcPath);
      Exit;
    end;
  end
  else
  begin
    if (ExpectedSizeFor(FileName) < 0) or
       (not FileSize64(SrcPath, ActualSize)) or
       (ActualSize <> ExpectedSizeFor(FileName)) then
    begin
      Log('Size mismatch — discarding incomplete download: ' + SrcPath);
      DeleteFile(SrcPath);
      Exit;
    end;
  end;
  if (Pos('kokoro', FileName) > 0) or (Pos('voices', FileName) > 0) then
    SubDir := '\voice\'
  else
    SubDir := '\';
  DestPath := ExpandConstant('{code:ModelsDir}') + SubDir + FileName;
  ForceDirectories(ExtractFileDir(DestPath));
  if RenameFile(SrcPath, DestPath) then
    Log('Staged verified download (moved): ' + DestPath)
  else if CopyFile(SrcPath, DestPath, False) then
    Log('Staged verified download (copied): ' + DestPath)
  else
  begin
    Log('Could not stage verified download: ' + DestPath);
    Exit;
  end;
  if FileName = '{#Qwen4FileName}' then
    CatalogQwen4()
  else if FileName = '{#Qwen8FileName}' then
    CatalogQwen8()
  else if FileName = '{#Qwen14FileName}' then
    CatalogQwen14()
  else if FileName = '{#Qwen30FileName}' then
    CatalogQwen30()
  else if FileName = 'kokoro-v1.0.onnx' then
    CatalogKokoroModel()
  else if FileName = 'voices-v1.0.bin' then
    CatalogKokoroVoices();
end;

procedure StageVerifiedDownloads();
// Called when the download page exits — success, abort, or failure. Downloads
// carry no Inno-side hash check (it made the UI go deaf for tens of seconds
// between files), so integrity is enforced HERE: hash each completed file and
// only move + catalog it when the SHA-256 matches. An aborted run then keeps
// every verified model: a restart finds them trusted and skips them.
var
  I: Integer;
begin
  if DlCompleted = nil then
    Exit;
  for I := 0 to DlCompleted.Count - 1 do
  begin
    // Renames are instant on the install drive, but hashing the source is
    // still multi-GB work — keep the page honest about what it is doing.
    if (not WizardSilent()) and (DownloadTotalLabel <> nil) then
    begin
      DownloadTotalLabel.Caption :=
        'Storing completed downloads (' + IntToStr(I + 1) + ' of ' +
        IntToStr(DlCompleted.Count) + '): ' + DlCompleted[I] + '...';
      DownloadTotalLabel.Update;
    end;
    MoveVerifiedDownload(DlCompleted[I], False);
  end;
end;

procedure SweepStagedDownloads();
// The staging dir survives restarts (it lives under models\.dl). Any complete
// file left by a crashed or aborted run is SHA-256 verified and moved into
// models\ now so the trust checks below skip re-downloading it. Deep verify:
// a leftover could be a truncated write from a killed process.
begin
  MoveVerifiedDownload('{#Qwen4FileName}', True);
  MoveVerifiedDownload('{#Qwen8FileName}', True);
  MoveVerifiedDownload('{#Qwen14FileName}', True);
  MoveVerifiedDownload('{#Qwen30FileName}', True);
  MoveVerifiedDownload('kokoro-v1.0.onnx', True);
  MoveVerifiedDownload('voices-v1.0.bin', True);
end;

function EnsureDlDiskSpace(): Boolean;
// Pre-flight the drive that will actually hold the download bytes. Past
// builds queued multi-GB pulls into C:\Temp and died mid-run with a raw
// "not enough space" stream error — check first and explain it instead.
var
  CheckDrive: String;
  FreeBytes, TotalBytes, NeededBytes: Int64;
begin
  Result := True;
  // 2 GB allowance for the app payload itself; staged downloads are moved
  // (not duplicated) so DlTotalBytes is the real peak requirement.
  NeededBytes := DlTotalBytes + (Int64(2) * 1073741824);
  if DlStagingRedirected then
    CheckDrive := ExtractFileDrive(ExpandConstant('{code:ModelsDir}')) + '\'
  else
    CheckDrive := ExtractFileDrive(ExpandConstant('{tmp}')) + '\';
  if not GetSpaceOnDisk64(CheckDrive, FreeBytes, TotalBytes) then
    Exit;
  if FreeBytes >= NeededBytes then
    Exit;
  Log('Insufficient space on ' + CheckDrive + ': need ' +
    IntToStr(NeededBytes) + ' bytes, have ' + IntToStr(FreeBytes) + '.');
  SuppressibleMsgBox(
    'Drive ' + CheckDrive + ' does not have enough free space for this ' +
    'installation.' + #13#10 + #13#10 +
    'Required: ' + IntToStr(NeededBytes div 1073741824) + ' GB' + #13#10 +
    'Available: ' + IntToStr(FreeBytes div 1073741824) + ' GB' + #13#10 + #13#10 +
    'Free up space on this drive and run Setup again.',
    mbCriticalError, MB_OK, IDOK);
  Result := False;
end;

procedure PerformModelDownloadsSilent();
begin
  if SkipModelDownloads then
    Exit;
  EnsureDlStaging();
  SweepStagedDownloads();
  EvaluateModelChecks();
  // Compute the same total the interactive queue uses, then refuse to pull
  // on a drive that cannot fit it — silent runs get a log line instead of a
  // raw stream-write crash mid-download.
  DlTotalBytes := 0;
  if DoQwen4 then DlTotalBytes := DlTotalBytes + {#Qwen4Size};
  if DoQwen8 then DlTotalBytes := DlTotalBytes + {#Qwen8Size};
  if DoQwen14 then DlTotalBytes := DlTotalBytes + {#Qwen14Size};
  if DoQwen30 then DlTotalBytes := DlTotalBytes + {#Qwen30Size};
  if DoKokoroModel then DlTotalBytes := DlTotalBytes + {#KokoroModelSize};
  if DoKokoroVoices then DlTotalBytes := DlTotalBytes + {#KokoroVoicesSize};
  if (DlTotalBytes > 0) and (not EnsureDlDiskSpace()) then
  begin
    Log('Silent mode: insufficient disk space — skipping model downloads.');
    Exit;
  end;
  if DoQwen4 then
    DownloadTemporaryFile('{#Qwen4Url}', 'dl\{#Qwen4FileName}', '{#Qwen4Sha256}', @ModelDlProgress);
  if DoQwen8 then
    DownloadTemporaryFile('{#Qwen8Url}', 'dl\{#Qwen8FileName}', '{#Qwen8Sha256}', @ModelDlProgress);
  if DoQwen14 then
    DownloadTemporaryFile('{#Qwen14Url}', 'dl\{#Qwen14FileName}', '{#Qwen14Sha256}', @ModelDlProgress);
  if DoQwen30 then
    DownloadTemporaryFile('{#Qwen30Url}', 'dl\{#Qwen30FileName}', '{#Qwen30Sha256}', @ModelDlProgress);
  if DoKokoroModel then
    DownloadTemporaryFile('{#KokoroModelUrl}', 'dl\kokoro-v1.0.onnx', '{#KokoroModelSha256}', @ModelDlProgress);
  if DoKokoroVoices then
    DownloadTemporaryFile('{#KokoroVoicesUrl}', 'dl\voices-v1.0.bin', '{#KokoroVoicesSha256}', @ModelDlProgress);
  StageVerifiedDownloads();
end;


// Returns False when the user aborted the download page (stay on Ready).
function PerformModelDownloads(): Boolean;
var
  Attempt: Integer;
  Done: Boolean;
begin
  Result := True;
  if SkipModelDownloads then
  begin
    Log('Model downloads skipped (CHAT_NEXUS_SKIP_MODEL_DOWNLOADS=1).');
    Exit;
  end;

  // Show the page BEFORE the trust checks: hashing leftover multi-GB models
  // takes tens of seconds and would otherwise leave the Ready page frozen.
  DownloadPage.Show;
  try
    DownloadPage.SetText(
      'Checking existing models',
      'Verifying models already on disk. Large files can take a minute to ' +
        'check — Setup is working, please wait.');
    // Downloads stage in {tmp}\dl — junctioned onto the install drive, which
    // is the disk the user actually sized for. Then recover any completed
    // downloads left in the staging dir by an earlier run before deciding
    // what still needs fetching.
    EnsureDlStaging();
    SweepStagedDownloads();
    EvaluateModelChecks();
    if not AnyModelQueued() then
    begin
      Log('All planned models already installed and trusted; nothing to download.');
      Exit;
    end;

    DownloadPage.SetText(
      'Downloading models',
      'Fetching the models selected for this hardware. Models already on ' +
        'disk and verified are skipped.');
    QueueModelDownloads();
    if not EnsureDlDiskSpace() then
    begin
      Log('Aborting download queue: insufficient disk space.');
      Result := False;
      DlForceExit := True;
      Exit;
    end;

    // One automatic retry — transient CDN/proxy hiccups ("internal error",
    // dropped connections) are common on multi-GB pulls and should not
    // punt the user back to the Ready page for no reason. Files already
    // verified in {tmp} are skipped on the retry. (While/done-flag loop:
    // Break inside except does not reliably exit a for loop in Pascal
    // Script — a user abort was being retried as a failure.)
    Attempt := 0;
    Done := False;
    while not Done do
    begin
      try
        DownloadPage.Download;
        Done := True;
      except
        if DownloadPage.AbortedByUser then
        begin
          Log('Model downloads aborted by user.');
          Result := False;
          Done := True;
        end
        else if Attempt = 0 then
        begin
          Attempt := 1;
          Log('Download failed, retrying once: ' + GetExceptionMessage);
        end
        else
        begin
          Result := SuppressibleMsgBox(
            'A model download failed:' + #13#10 + GetExceptionMessage + #13#10 + #13#10 +
            'Continue the installation anyway? Missing models can be fetched ' +
            'later from the app.', mbConfirmation, MB_YESNO, IDYES) = IDYES;
          Done := True;
        end;
      end;
    end;
  finally
    // Success, abort, or failure — keep every file Inno already verified.
    StageVerifiedDownloads();
    DownloadPage.Hide;
  end;

  // An aborted download (or a failed disk-space pre-flight) should not
  // silently bounce back to the Ready page — route through the native cancel
  // confirmation ("Setup is not complete — exit?") so Yes closes the whole
  // installer. Choosing No still lands back on Ready with every verified
  // download already staged and skipped next run.
  if (not Result) and (not WizardSilent()) and
     (DownloadPage.AbortedByUser or DlForceExit) then
    WizardForm.Close;
end;

// Silent installs never reach NextButtonClick — run the same queue
// headlessly when the install step starts.
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

  // Leftover files without a registered uninstall entry are NOT an
  // upgrade — a partial uninstall that leaves NexusCore.exe behind must
  // not brand the next run as "Update" (that was a reported regression:
  // fresh installs showed the update flow). State is still preserved by
  // the [Files] excludes; the process sweep is unconditional.
  DefaultPath := PreferredInstallDir('');
  if not FileExists(AddBackslash(DefaultPath) + '{#AppExeName}') then
    DefaultPath := ExpandConstant('{localappdata}\Programs\Nexus Core');
  if not FileExists(AddBackslash(DefaultPath) + '{#AppExeName}') then
    DefaultPath := ExpandConstant('{localappdata}\Programs\Chat Nexus');
  if FileExists(AddBackslash(DefaultPath) + '{#AppExeName}') or
     FileExists(AddBackslash(DefaultPath) + 'ChatNexus.exe') then
  begin
    ExistingVersion := '';
    ExistingInstallDir := DefaultPath;
    Log('Unregistered files found at ' + DefaultPath +
        ' — proceeding as a fresh install over the leftovers.');
  end;
end;

procedure ProbeHardware();
// Reads installed RAM (kernel32) plus the largest dedicated GPU VRAM
// reported under the display-adapter class key, then pre-selects the
// model-download page defaults for this machine. Recommendations only —
// every model stays user-checkable.
var
  TotalKB, MaxVram, V: Int64;
  D: Cardinal;
  S: AnsiString;
  SubKey, GpuClassKey: String;
  Names: TArrayOfString;
  I: Integer;
begin
  TotalKB := 0;
  if not GetPhysicallyInstalledSystemMemory(TotalKB) then
    TotalKB := 0;
  RamGB := TotalKB div 1048576;

  GpuClassKey :=
    'SYSTEM\CurrentControlSet\Control\Class\' +
    '{4D36E968-E325-11CE-BFC1-08002BE10318}';
  MaxVram := 0;
  GpuDesc := '';
  if RegGetSubkeyNames(HKLM, GpuClassKey, Names) then
  begin
    for I := 0 to GetArrayLength(Names) - 1 do
    begin
      SubKey := GpuClassKey + '\' + Names[I];
      V := 0;
      if RegQueryBinaryValue(HKLM, SubKey,
           'HardwareInformation.qwMemorySize', S) and (Length(S) >= 8) then
        V := Int64(Ord(S[1])) or (Int64(Ord(S[2])) shl 8) or
             (Int64(Ord(S[3])) shl 16) or (Int64(Ord(S[4])) shl 24) or
             (Int64(Ord(S[5])) shl 32) or (Int64(Ord(S[6])) shl 40) or
             (Int64(Ord(S[7])) shl 48) or (Int64(Ord(S[8])) shl 56)
      else if RegQueryBinaryValue(HKLM, SubKey,
                'HardwareInformation.MemorySize', S) and (Length(S) = 4) then
        V := Int64(Ord(S[1])) or (Int64(Ord(S[2])) shl 8) or
             (Int64(Ord(S[3])) shl 16) or (Int64(Ord(S[4])) shl 24)
      else if RegQueryDWordValue(HKLM, SubKey,
                'HardwareInformation.MemorySize', D) then
        V := D;
      if V > MaxVram then
      begin
        MaxVram := V;
        RegQueryStringValue(HKLM, SubKey, 'DriverDesc', GpuDesc);
      end;
    end;
  end;
  VramGB := MaxVram div 1073741824;

  CpuDesc := '';
  RegQueryStringValue(
    HKLM,
    'HARDWARE\DESCRIPTION\System\CentralProcessor\0',
    'ProcessorNameString', CpuDesc);
  CpuDesc := Trim(CpuDesc);
  CoreCount := StrToIntDef(GetEnv('NUMBER_OF_PROCESSORS'), 0);

  // Q4_K_M fit, smallest to largest. The 30B is a MoE that llama.cpp can
  // run hybrid — hot expert layers on the GPU, the rest in system RAM —
  // so combined VRAM+RAM qualifies it, not VRAM alone.
  RecQwen4 := (VramGB >= 4) or (RamGB >= 6);
  RecQwen8 := (VramGB >= 8) or (RamGB >= 12);
  RecQwen14 := (VramGB >= 12) or (RamGB >= 16);
  RecQwen30 := (VramGB >= 24) or ((RamGB + VramGB) >= 40);
  Log('Hardware scan: CPU=' + CpuDesc + ' (' + IntToStr(CoreCount) +
      ' threads), RAM=' + IntToStr(RamGB) + ' GB, GPU=' + GpuDesc +
      ' (' + IntToStr(VramGB) + ' GB VRAM); recommend 4B=' +
      IntToStr(Ord(RecQwen4)) + ' 8B=' + IntToStr(Ord(RecQwen8)) +
      ' 14B=' + IntToStr(Ord(RecQwen14)) + ' 30B=' + IntToStr(Ord(RecQwen30)));
end;

function InitializeSetup(): Boolean;
var
  Prompt: String;
begin
  ProbeHardware();
  SkipModelDownloads := CompareText(GetEnv('CHAT_NEXUS_SKIP_MODEL_DOWNLOADS'), '1') = 0;
  UpgradeDetected := DetectExistingInstall();
  Result := True;

  if SkipModelDownloads then
    Log('CHAT_NEXUS_SKIP_MODEL_DOWNLOADS=1; installer bootstrap downloads are disabled for this run.');

  if UpgradeDetected and (not WizardSilent()) then
  begin
    Prompt :=
      'Nexus Core is already installed.' + #13#10 + #13#10 +
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
  MessageText, Plan: String;
  TotalBytes: Int64;
begin
  InitializeDownloadPage();

  if UpgradeDetected then
  begin
    WizardForm.Caption := 'Update Nexus Core';
    WizardForm.WelcomeLabel1.Caption := 'Update Nexus Core';
    WizardForm.WelcomeLabel2.Caption :=
      'This will update Nexus Core on your computer.';

    MessageText :=
      'Setup detected an existing Nexus Core installation.' + #13#10 + #13#10 +
      'Existing version: ' + ExistingVersion + #13#10 +
      'Update version: {#AppVersion}' + #13#10 + #13#10 +
      'The application and bundled runtime will be updated in place.' + #13#10 +
      'Models, config.json, task/data files, and your Source Git workspace are preserved.';

    UpgradeInfoPage := CreateOutputMsgPage(
      wpWelcome,
      'Update detected',
      'Your existing Nexus Core installation will be updated.',
      MessageText
    );

    UninstallButton := TNewButton.Create(WizardForm);
    UninstallButton.Parent := WizardForm;
    UninstallButton.Caption := 'Uninstall existing version';
    UninstallButton.Width := WizardForm.CancelButton.Width + ScaleX(60);
    UninstallButton.Height := WizardForm.CancelButton.Height;
    UninstallButton.Top := WizardForm.CancelButton.Top;
    UninstallButton.Left :=
      WizardForm.ClientWidth - WizardForm.CancelButton.Left -
      WizardForm.CancelButton.Width;
    UninstallButton.OnClick := @UninstallButtonClick;
  end;

  if GpuDesc = '' then
    GpuDesc := 'unknown adapter';
  Plan := '';
  TotalBytes := 0;
  if RecQwen4 then
  begin
    Plan := Plan + '  Qwen3 4B Instruct — fastest, quick tasks (2.4 GB)' + #13#10;
    TotalBytes := TotalBytes + {#Qwen4Size};
  end;
  if RecQwen8 then
  begin
    Plan := Plan + '  Qwen3 8B — balanced everyday tasks (4.9 GB)' + #13#10;
    TotalBytes := TotalBytes + {#Qwen8Size};
  end;
  if RecQwen14 then
  begin
    Plan := Plan + '  Qwen3 14B — general agent model (8.9 GB)' + #13#10;
    TotalBytes := TotalBytes + {#Qwen14Size};
  end;
  if RecQwen30 then
  begin
    Plan := Plan + '  Qwen3-Coder 30B-A3B — deep coding, hybrid GPU+CPU (18.2 GB)' + #13#10;
    TotalBytes := TotalBytes + {#Qwen30Size};
  end;
  if Plan = '' then
    Plan := '  None recommended for this hardware — the app can fetch ' +
            'models later from the Tools page.' + #13#10;
  ModelPlanPage := CreateOutputMsgPage(
    wpWelcome,
    'Hardware scan & model downloads',
    'Setup scanned this hardware and auto-selected the models below.',
    '  CPU: ' + CpuDesc + ' (' + IntToStr(CoreCount) + ' threads)' + #13#10 +
    '  GPU: ' + GpuDesc + ' — ' + IntToStr(VramGB) + ' GB VRAM' + #13#10 +
    '  System RAM: ' + IntToStr(RamGB) + ' GB' + #13#10 + #13#10 +
    '  Models that will be downloaded:' + #13#10 + Plan + #13#10 +
    '  Total download: ' + Format('%.1f', [TotalBytes / 1073741824.0]) +
      ' GB' + #13#10 +
    '  Total install size (app + models): ~' +
      Format('%.1f', [(TotalBytes + 850000000) / 1073741824.0]) +
      ' GB' + #13#10 + #13#10 +
    '  The app routes each request to the smallest model that can ' +
      'handle it.'
  );
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

// taskkill /IM matches on image name only. An orphaned backend (started by a
// previous host launch, or by a leftover helper the desktop host already lost
// track of) can survive the name sweep while still holding {app} files open.
// Two backstops close that gap:
//  - the backend.pid file the desktop host writes after every backend launch
//  - a PowerShell sweep that stops ANY process whose executable lives under
//    the install directory, regardless of image name
procedure KillBackendFromPidFile();
var
  PidPath, PidText: String;
  Content: AnsiString;
  SepPos, Pid, ResultCode: Integer;
begin
  PidPath := ExpandConstant('{app}\data\logs\backend.pid');
  if not FileExists(PidPath) then
    Exit;
  if not LoadStringFromFile(PidPath, Content) then
    Exit;
  SepPos := Pos('|', Content);
  if SepPos > 0 then
    PidText := Copy(Content, 1, SepPos - 1)
  else
    PidText := Content;
  Pid := StrToIntDef(Trim(PidText), 0);
  if Pid <= 0 then
    Exit;
  if Exec('taskkill.exe', '/PID ' + IntToStr(Pid) + ' /T /F', '',
          SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log('taskkill on recorded backend pid ' + IntToStr(Pid) +
        ' returned ' + IntToStr(ResultCode))
  else
    Log('Could not execute taskkill for recorded backend pid ' + IntToStr(Pid));
end;

procedure StopProcessesUnderInstallDir();
var
  PsCommand: String;
  ResultCode: Integer;
begin
  PsCommand :=
    'Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith(''' +
    AddBackslash(ExpandConstant('{app}')) +
    ''') } | Stop-Process -Force';
  if not Exec('powershell.exe',
              '-NoProfile -ExecutionPolicy Bypass -Command "' + PsCommand + '"',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log('Could not execute PowerShell install-dir process sweep.')
  else if ResultCode <> 0 then
    Log('PowerShell install-dir process sweep returned ' + IntToStr(ResultCode));
end;

// Opening an exe for write fails while a process is running it — the same
// lock that produces "DeleteFile failed; code 5" during file replacement.
function FileIsWriteLocked(const Path: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := True;
  if not FileExists(Path) then
  begin
    Result := False;
    Exit;
  end;
  try
    Stream := TFileStream.Create(Path, fmOpenReadWrite or fmShareDenyNone);
    try
      Stream.Free;
      Result := False;
    except
    end;
  except
  end;
end;

function InstallFilesLocked(): Boolean;
begin
  Result :=
    FileIsWriteLocked(ExpandConstant('{app}\{#AppExeName}')) or
    FileIsWriteLocked(ExpandConstant('{app}\backend\ChatNexus.Backend.exe')) or
    FileIsWriteLocked(ExpandConstant('{app}\runtime\llama\llama-server.exe'));
end;

procedure WaitForInstallFilesUnlock();
var
  Seconds: Integer;
begin
  Seconds := 0;
  while InstallFilesLocked() and (Seconds < 45) do
  begin
    if Seconds = 5 then
      StopProcessesUnderInstallDir();  // escalate once early, then keep waiting
    ShowBusyStatus(
      'Waiting for Nexus Core files to be released...',
      'A running process still has application files open. Setup keeps ' +
        'retrying for up to 45 seconds (' + IntToStr(Seconds) + 's).');
    BusySleep(1000);
    Seconds := Seconds + 1;
  end;
  if InstallFilesLocked() then
  begin
    Log('Install files still locked after waiting; Inno file-replace retry will prompt the user.');
    ShowBusyStatus(
      'Nexus Core files are still in use',
      'Please close every Nexus Core window and try again. If the problem ' +
        'persists, restart the computer and run this setup again.');
  end;
end;

procedure StopRunningNexusCore();
begin
  // Always sweep — never gate on UpgradeDetected. A running app survives
  // its own uninstaller (locked exes are left behind), so an
  // uninstall+reinstall leaves NexusCore.exe holding {app} files open and
  // the "fresh" install hits DeleteFile code 5.
  Log('Closing any running Nexus Core processes before replacing files.');

  // Avoid Restart Manager for Nexus Core because the desktop host owns a hidden
  // backend and llama.cpp child process. Close the desktop tree first, allow a
  // short grace period, then force-clean any orphaned children.
  ShowBusyStatus(
    'Closing Nexus Core...',
    'Setup is asking the running application to shut down.');
  TaskKillImage('{#AppExeName}', False);
  TaskKillImage('ChatNexus.exe', False);  // legacy exe name from pre-Nexus-Core installs
  ShowBusyStatus(
    'Waiting for Nexus Core to exit...',
    'Giving the backend and AI runtime a moment to release files.');
  BusySleep(1500);
  ShowBusyStatus(
    'Stopping remaining Nexus Core processes...',
    'Cleaning up the backend and runtime so files can be replaced.');
  TaskKillImage('{#AppExeName}', True);
  TaskKillImage('ChatNexus.exe', True);
  TaskKillImage('ChatNexus.Backend.exe', True);
  TaskKillImage('llama-server.exe', True);
  TaskKillImage('llama.exe', True);
  KillBackendFromPidFile();
  StopProcessesUnderInstallDir();
  BusySleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if UpgradeDetected then
    ShowBusyStatus(
      'Preparing update...',
      'Setup is closing Nexus Core and checking the existing installation. Please wait.');
  StopRunningNexusCore();
  // The unlock wait is cheap when nothing holds files — run it on every
  // install path, not just detected upgrades (uninstall survivors lock
  // files the same way).
  WaitForInstallFilesUnlock();
  if UpgradeDetected then
    ShowBusyStatus(
      'Analyzing files to update...',
      'Setup is building the list of files to replace. Extraction will ' +
        'begin shortly and progress will move to the bar below.');
  InstallBundledSource := not FileExists(ExpandConstant('{app}\Source\.git\HEAD'));

  if InstallBundledSource then
    Log('No existing Source Git workspace detected; installing the bundled workspace.')
  else
    Log('Existing Source Git workspace detected; preserving it unchanged during update.');

  // Interactive installs download on the TDownloadWizardPage shown from
  // NextButtonClick (wpReady) — it stays responsive and abortable. Silent
  // installs never see that page, so fetch headlessly here instead.
  if WizardSilent() then
    PerformModelDownloadsSilent();

  Result := '';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then
    Result := PerformModelDownloads();
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpInstalling then
    WizardForm.PageNameLabel.Caption := 'Installing Nexus Core';

  if UninstallButton <> nil then
    UninstallButton.Visible := UpgradeDetected and
      ((CurPageID <= wpSelectDir) or (CurPageID = UpgradeInfoPage.ID));

  if not UpgradeDetected then
    Exit;

  if CurPageID = wpSelectDir then
  begin
    WizardForm.PageNameLabel.Caption := 'Select Update Location';
    WizardForm.PageDescriptionLabel.Caption :=
      'Where should the Nexus Core update be applied?';
    WizardForm.SelectDirLabel.Caption :=
      'Setup will update Nexus Core in the following folder.';
    WizardForm.SelectDirBrowseLabel.Caption :=
      'To continue, click Next. To update in a different folder, click Browse.';
  end;

  if CurPageID = wpReady then
  begin
    WizardForm.NextButton.Caption := '&Update';
    WizardForm.PageNameLabel.Caption := 'Ready to Update';
    WizardForm.ReadyLabel.Caption :=
      'Setup is now ready to begin updating Nexus Core on your computer.';
  end;

  if CurPageID = wpInstalling then
    WizardForm.StatusLabel.Caption := 'Updating Nexus Core...';

  if CurPageID = wpFinished then
  begin
    WizardForm.FinishedHeadingLabel.Caption := 'Completing Nexus Core Update';
    WizardForm.FinishedLabel.Caption :=
      'Setup has finished updating Nexus Core on your computer.' + #13#10 + #13#10 +
      'The application may be launched by selecting the installed shortcut.';
  end;
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
      CopyFile(ExampleConfig, UserConfig, False);

    InstallCompleted := True;

    // Keep installer-downloaded models recognized as verified by Nexus Core.
    if FileExists(ExpandConstant('{code:ModelsDir}\{#Qwen4FileName}')) then
      WriteCatalogMetadata(
        '{#Qwen4CatalogId}',
        '{#Qwen4FileName}',
        '{#Qwen4Sha256}',
        {#Qwen4Size},
        '{#Qwen4SourceRepo}');

    if FileExists(ExpandConstant('{code:ModelsDir}\{#Qwen8FileName}')) then
      WriteCatalogMetadata(
        '{#Qwen8CatalogId}',
        '{#Qwen8FileName}',
        '{#Qwen8Sha256}',
        {#Qwen8Size},
        '{#Qwen8SourceRepo}');

    if FileExists(ExpandConstant('{code:ModelsDir}\{#Qwen14FileName}')) then
      WriteCatalogMetadata(
        '{#Qwen14CatalogId}',
        '{#Qwen14FileName}',
        '{#Qwen14Sha256}',
        {#Qwen14Size},
        '{#Qwen14SourceRepo}');

    if FileExists(ExpandConstant('{code:ModelsDir}\{#Qwen30FileName}')) then
      WriteCatalogMetadata(
        '{#Qwen30CatalogId}',
        '{#Qwen30FileName}',
        '{#Qwen30Sha256}',
        {#Qwen30Size},
        '{#Qwen30SourceRepo}');

    if FileExists(ExpandConstant('{code:ModelsDir}\voice\kokoro-v1.0.onnx')) then
      WriteCatalogMetadata(
        'kokoro-v1-0-onnx',
        'kokoro-v1.0.onnx',
        '{#KokoroModelSha256}',
        {#KokoroModelSize},
        'hexgrad/Kokoro-82M');

    if FileExists(ExpandConstant('{code:ModelsDir}\voice\voices-v1.0.bin')) then
      WriteCatalogMetadata(
        'kokoro-voices-v1-0',
        'voices-v1.0.bin',
        '{#KokoroVoicesSha256}',
        {#KokoroVoicesSize},
        'hexgrad/Kokoro-82M');
  end;
end;

var
  // Set by the uninstall dialog — what to remove besides the program
  // itself. All default to delete: the user's standing complaint was
  // that uninstall left everything behind.
  UnDelProfile, UnDelBrain, UnDelModels, UnDelTools: Boolean;

procedure AskUninstallScope;
var
  Form: TForm;
  Lbl: TLabel;
  ChkProfile, ChkBrain, ChkModels, ChkTools: TCheckBox;
  Btn: TButton;
begin
  UnDelProfile := True;
  UnDelBrain := True;
  UnDelModels := True;
  UnDelTools := True;
  Form := TForm.Create(nil);
  try
    Form.Caption := '{#AppName} — choose what to remove';
    Form.BorderStyle := bsDialog;
    Form.Position := poScreenCenter;
    Form.ClientWidth := 400;
    Form.ClientHeight := 210;
    Lbl := TLabel.Create(Form);
    Lbl.Parent := Form;
    Lbl.Caption := 'The program is removed either way. Also remove:';
    Lbl.Left := 16;
    Lbl.Top := 12;
    Lbl.Width := 370;
    ChkProfile := TCheckBox.Create(Form);
    ChkProfile.Parent := Form;
    ChkProfile.Caption := 'Profile (accounts, personas, preferences)';
    ChkProfile.Left := 24;
    ChkProfile.Top := 40;
    ChkProfile.Width := 360;
    ChkProfile.Checked := True;
    ChkBrain := TCheckBox.Create(Form);
    ChkBrain.Parent := Form;
    ChkBrain.Caption := 'Brain files (memory, learning, checkpoints)';
    ChkBrain.Left := 24;
    ChkBrain.Top := 64;
    ChkBrain.Width := 360;
    ChkBrain.Checked := True;
    ChkModels := TCheckBox.Create(Form);
    ChkModels.Parent := Form;
    ChkModels.Caption := 'Models (downloaded LLM/voice/image weights)';
    ChkModels.Left := 24;
    ChkModels.Top := 88;
    ChkModels.Width := 360;
    ChkModels.Checked := True;
    ChkTools := TCheckBox.Create(Form);
    ChkTools.Parent := Form;
    ChkTools.Caption := 'Tools (ComfyUI and other downloaded runtimes)';
    ChkTools.Left := 24;
    ChkTools.Top := 112;
    ChkTools.Width := 360;
    ChkTools.Checked := True;
    Btn := TButton.Create(Form);
    Btn.Parent := Form;
    Btn.Caption := 'Continue uninstall';
    Btn.ModalResult := mrOk;
    Btn.Default := True;
    Btn.Left := 140;
    Btn.Top := 156;
    Btn.Width := 130;
    if Form.ShowModal = mrOk then
    begin
      UnDelProfile := ChkProfile.Checked;
      UnDelBrain := ChkBrain.Checked;
      UnDelModels := ChkModels.Checked;
      UnDelTools := ChkTools.Checked;
    end;
  finally
    Form.Free;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  // A running NexusCore.exe holds {app} files open; uninstall would
  // silently leave them behind (locked files can't be deleted), and the
  // surviving process then blocks the next install with DeleteFile
  // code 5. Stop the app tree before any file is touched. Plain Sleep —
  // WizardForm/BusySleep don't exist in the uninstaller.
  TaskKillImage('{#AppExeName}', False);
  TaskKillImage('ChatNexus.exe', False);
  Sleep(1500);
  TaskKillImage('{#AppExeName}', True);
  TaskKillImage('ChatNexus.exe', True);
  TaskKillImage('ChatNexus.Backend.exe', True);
  TaskKillImage('llama-server.exe', True);
  TaskKillImage('llama.exe', True);
  KillBackendFromPidFile();
  StopProcessesUnderInstallDir();
  AskUninstallScope;
  Result := True;
end;

procedure PurgeLeftoverInstallDir;
// TFindRec.Attributes bit values (winnt.h): $10 = directory, $400 = reparse
// point (junction/symlink). Pascal Script has no named constants for these.
var
  FindRec: TFindRec;
  AppDir, ItemPath: String;
begin
  // Inno only removes files it tracked at install time. Everything the app
  // created afterwards — the state junctions (data/.agent/output), real
  // models/tools dirs, generated files, downloaded runtime extras — would
  // keep {app} alive and make uninstall look like a no-op. Sweep it all.
  // Reparse points are unlinked, never traversed: RemoveDir on a junction
  // deletes the link itself, leaving the external target (user state,
  // model files) untouched.
  AppDir := ExpandConstant('{app}');
  if FindFirst(AppDir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') and
           (CompareText(FindRec.Name, 'unins000.exe') <> 0) and
           (CompareText(FindRec.Name, 'unins000.dat') <> 0) and
           (UnDelModels or (CompareText(FindRec.Name, 'models') <> 0)) and
           (UnDelTools or (CompareText(FindRec.Name, 'tools') <> 0)) then
        begin
          ItemPath := AppDir + '\' + FindRec.Name;
          if (FindRec.Attributes and $10) <> 0 then
          begin
            if (FindRec.Attributes and $400) <> 0 then
              RemoveDir(ItemPath)
            else
              DelTree(ItemPath, True, True, True);
          end
          else
            DeleteFile(ItemPath);
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
  // If anything survived (locked file, odd attributes) the app dir stays —
  // remove what Inno can and let its own final cleanup drop {app}.
  RemoveDir(AppDir);
end;

function KeepStateEntry(Name: String): Boolean;
// Entries under {localappdata}\NexusCore\data the user asked to keep.
begin
  Result := (not UnDelProfile and (CompareText(Name, 'profiles') = 0)) or
            (not UnDelBrain and
             ((CompareText(Name, 'brain') = 0) or
              (CompareText(Name, 'nexus_brain') = 0)));
end;

procedure PurgeUserState;
// Junctions inside {app} were unlinked by PurgeLeftoverInstallDir — the
// real state lives under %LOCALAPPDATA%\NexusCore. Delete it honoring
// the profile/brain checkboxes.
var
  FindRec: TFindRec;
  StateRoot, DataDir, ItemPath: String;
begin
  StateRoot := ExpandConstant('{localappdata}\NexusCore');
  DataDir := StateRoot + '\data';
  if FindFirst(DataDir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') and
           not KeepStateEntry(FindRec.Name) then
        begin
          ItemPath := DataDir + '\' + FindRec.Name;
          if (FindRec.Attributes and $10) <> 0 then
            DelTree(ItemPath, True, True, True)
          else
            DeleteFile(ItemPath);
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
  // .agent = checkpoints/memory/tasks → covered by the brain checkbox;
  // output = generated artifacts → always removed with the app.
  if UnDelBrain then
    DelTree(StateRoot + '\.agent', True, True, True);
  DelTree(StateRoot + '\output', True, True, True);
  // Legacy models also lived at {drive}:\NexusCore\models before the
  // single-root re-home — honor the models checkbox there too.
  if UnDelModels then
  begin
    DelTree(ExtractFileDrive(ExpandConstant('{app}')) + '\NexusCore\models', True, True, True);
    RemoveDir(ExtractFileDrive(ExpandConstant('{app}')) + '\NexusCore');
  end;
  RemoveDir(DataDir);
  RemoveDir(StateRoot);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // Belt and braces: anything spawned mid-uninstall (health watchdog,
    // tray relaunch) re-locks files before the sweep.
    TaskKillImage('{#AppExeName}', True);
    TaskKillImage('ChatNexus.exe', True);
    TaskKillImage('ChatNexus.Backend.exe', True);
    TaskKillImage('llama-server.exe', True);
    TaskKillImage('llama.exe', True);
    KillBackendFromPidFile();
    StopProcessesUnderInstallDir();
    PurgeLeftoverInstallDir;
    // Junctions are gone — targets under %LOCALAPPDATA% are real dirs now.
    PurgeUserState;
  end;
end;
