param(
    [string]$Python = "python",
    [string]$LlamaTag = "b11278"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

Write-Host "Syncing version artifacts from VERSION..."
& $Python "scripts\sync_version.py"
if ($LASTEXITCODE -ne 0) { throw "version sync failed" }

$LlamaAsset = "llama-$LlamaTag-bin-win-vulkan-x64.zip"
$LlamaUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaTag/$LlamaAsset"
$LlamaSha256 = "28800e708e4e4cc31dd77775d4ab34ddf000b5c3eb5da8fecf90308418540fd5"
$RuntimeArchive = Join-Path $Root "packaging\llama-runtime.zip"
$RuntimeExtract = Join-Path $Root "packaging\llama-runtime"
$PackageRoot = Join-Path $Root "dist\ChatNexus"
$BackendDist = Join-Path $Root "build\backend-dist"
$BackendWork = Join-Path $Root "build\backend-work"
$DesktopPublish = Join-Path $Root "build\desktop-publish"
$DesktopProject = Join-Path $Root "desktop\ChatNexus.Desktop\ChatNexus.Desktop.csproj"
$DesktopIcon = Join-Path $Root "desktop\ChatNexus.Desktop\nexus-core.ico"
$DesktopSplash = Join-Path $Root "desktop\ChatNexus.Desktop\nexus-core-splash.png"

# Running out of dist/ is supported for dev loops, so its mutable state is
# real user data — merge it into the per-user state root before wiping, or
# every rebuild would silently delete chat history, memory, and output.
$StateRoot = Join-Path $env:LOCALAPPDATA "NexusCore"
# Small state lives in the per-user profile; models can be tens of GB so the
# host redirects them to a shared root on the install drive instead — the
# merge below mirrors that split (see StateTargetRoot in Program.cs).
# On a clean checkout dist\ChatNexus doesn't exist yet — Resolve-Path would
# throw. PackageRoot is always under $Root, so fall back to $Root's drive.
$PackageDrive = if (Test-Path $PackageRoot) { (Resolve-Path $PackageRoot).Path } else { $Root }
$DriveStateRoot = Join-Path ([IO.Path]::GetPathRoot($PackageDrive)) "NexusCore"
$DriveDirs = @("models", "ComfyUI_windows_portable")
foreach ($stateDir in @("data", ".agent", "output", "models", "ComfyUI_windows_portable")) {
    $existing = Join-Path $PackageRoot $stateDir
    if (-not (Test-Path $existing)) { continue }
    $item = Get-Item $existing -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        # Junction into the state root — remove only the link; a recursive
        # Remove-Item on a reparse point can traverse into the target.
        cmd /c rmdir "$existing" | Out-Null
        continue
    }
    $dest = Join-Path $(if ($DriveDirs -contains $stateDir) { $DriveStateRoot } else { $StateRoot }) $stateDir
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Get-ChildItem $existing -Force | ForEach-Object {
        $d = Join-Path $dest $_.Name
        if (-not (Test-Path $d)) { Copy-Item $_.FullName $d -Recurse -Force }
    }
}
# A user-edited config.json in the build output survives rebuilds too.
$StashDir = Join-Path $env:TEMP ("nexus-build-stash-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $StashDir | Out-Null
$PreservedConfig = Join-Path $StashDir "config.json"
if (Test-Path (Join-Path $PackageRoot "config.json")) {
    Copy-Item (Join-Path $PackageRoot "config.json") $PreservedConfig -Force
}

Remove-Item -Recurse -Force "build","dist",$RuntimeExtract -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "build","dist" | Out-Null

Write-Host "Verifying Nexus Core brand assets..."
if (-not (Test-Path $DesktopIcon)) { throw "nexus-core.ico missing — the official multi-resolution icon must be committed" }
if (-not (Test-Path $DesktopSplash)) { throw "nexus-core-splash.png missing — the official splash artwork must be committed" }
& $Python -c "from PIL import Image; im=Image.open(r'$DesktopIcon'); req={(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)}; missing=req-set(im.ico.sizes()); assert not missing, f'ico missing sizes: {missing}'"
if ($LASTEXITCODE -ne 0) { throw "nexus-core.ico does not contain the required resolution layers" }

Write-Host "Ensuring local voice runtime dependencies (kokoro-onnx stack)..."
& $Python -m pip install --quiet "onnxruntime==1.30.0" "phonemizer==3.4.0" "espeakng-loader==0.2.4" "numpy>=1.26"
if ($LASTEXITCODE -ne 0) { throw "voice runtime dependency install failed" }
# kokoro-onnx declares Python <3.14; verified working on 3.14 locally.
& $Python -m pip install --quiet --ignore-requires-python "kokoro-onnx==0.6.1"
if ($LASTEXITCODE -ne 0) { throw "kokoro-onnx install failed" }

Write-Host "Building hidden Python agent backend..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --console `
    --name "ChatNexus.Backend" `
    --version-file "$Root\packaging\backend_version.txt" `
    --distpath $BackendDist `
    --workpath $BackendWork `
    --specpath "build" `
    --add-data "$Root\VERSION;." `
    --add-data "$Root\web;web" `
    --add-data "$Root\tools;tools" `
    --collect-submodules localcodeagent `
    --collect-submodules py7zr `
    --collect-all cryptography `
    --collect-all onnxruntime `
    --collect-all kokoro_onnx `
    --collect-all phonemizer `
    --collect-all espeakng_loader `
    --collect-all numpy `
    --add-data "$Root\localcodeagent\voice\official;localcodeagent/voice/official" `
    --hidden-import pybcj --hidden-import pyppmd --hidden-import pyzstd `
    --hidden-import brotli --hidden-import Brotli --hidden-import inflate64 `
    --hidden-import multivolumefile --hidden-import Cryptodome `
    "packaging/chat_nexus_backend_entry.py"
if ($LASTEXITCODE -ne 0) { throw "Nexus Core backend PyInstaller build failed" }
$BackendSource = Join-Path $BackendDist "ChatNexus.Backend"
if (-not (Test-Path (Join-Path $BackendSource "ChatNexus.Backend.exe"))) { throw "ChatNexus.Backend.exe was not produced" }

Write-Host "Publishing native .NET 8 WebView2 desktop host..."
dotnet publish $DesktopProject `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:PublishTrimmed=false `
    -p:DebugType=None `
    -p:DebugSymbols=false `
    -o $DesktopPublish
if ($LASTEXITCODE -ne 0) { throw "Native Nexus Core desktop publish failed" }
if (-not (Test-Path (Join-Path $DesktopPublish "NexusCore.exe"))) { throw "Native NexusCore.exe was not produced" }

New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
Copy-Item -Path (Join-Path $DesktopPublish "*") -Destination $PackageRoot -Recurse -Force
Copy-Item $DesktopIcon (Join-Path $PackageRoot "nexus-core.ico") -Force
Copy-Item $DesktopSplash (Join-Path $PackageRoot "nexus-core-splash.png") -Force

$BackendTarget = Join-Path $PackageRoot "backend"
New-Item -ItemType Directory -Force -Path $BackendTarget | Out-Null
Copy-Item -Path (Join-Path $BackendSource "*") -Destination $BackendTarget -Recurse -Force

Write-Host "Downloading pinned official llama.cpp Vulkan runtime $LlamaTag..."
Invoke-WebRequest -Uri $LlamaUrl -OutFile $RuntimeArchive
$ActualHash = (Get-FileHash -Algorithm SHA256 $RuntimeArchive).Hash.ToLowerInvariant()
if ($ActualHash -ne $LlamaSha256) { throw "llama.cpp archive checksum mismatch. Expected $LlamaSha256, got $ActualHash" }
Expand-Archive -Path $RuntimeArchive -DestinationPath $RuntimeExtract -Force
$Server = Get-ChildItem -Path $RuntimeExtract -Recurse -Filter "llama-server.exe" | Select-Object -First 1
if (-not $Server) { throw "Pinned llama.cpp archive did not contain llama-server.exe" }
$RuntimeTarget = Join-Path $PackageRoot "runtime\llama"
New-Item -ItemType Directory -Force -Path $RuntimeTarget | Out-Null
Copy-Item -Path (Join-Path $Server.Directory.FullName "*") -Destination $RuntimeTarget -Recurse -Force

Write-Host "Bundling verified Kokoro voice assets (hexgrad/Kokoro-82M v1.0, Apache-2.0)..."
$VoiceAssetCache = Join-Path $Root "packaging\voice-assets"
New-Item -ItemType Directory -Force -Path $VoiceAssetCache | Out-Null
$VoiceAssetTarget = Join-Path $PackageRoot "models\voice"
New-Item -ItemType Directory -Force -Path $VoiceAssetTarget | Out-Null
# SHA-256 must match localcodeagent/voice/assets.py ASSETS entries.
$VoiceAssets = @(
    @{ Name = "kokoro-v1.0.onnx"
       Url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
       Sha256 = "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5" },
    @{ Name = "voices-v1.0.bin"
       Url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
       Sha256 = "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d" }
)
foreach ($VoiceAsset in $VoiceAssets) {
    $Cached = Join-Path $VoiceAssetCache $VoiceAsset.Name
    $CachedOk = (Test-Path $Cached) -and
        ((Get-FileHash -Algorithm SHA256 $Cached).Hash.ToLowerInvariant() -eq $VoiceAsset.Sha256)
    if (-not $CachedOk) {
        Invoke-WebRequest -Uri $VoiceAsset.Url -OutFile $Cached
        $Actual = (Get-FileHash -Algorithm SHA256 $Cached).Hash.ToLowerInvariant()
        if ($Actual -ne $VoiceAsset.Sha256) {
            Remove-Item $Cached -Force
            throw "voice asset checksum mismatch for $($VoiceAsset.Name). Expected $($VoiceAsset.Sha256), got $Actual"
        }
    }
    Copy-Item $Cached (Join-Path $VoiceAssetTarget $VoiceAsset.Name) -Force
}

Copy-Item "config.example.json" (Join-Path $PackageRoot "config.example.json") -Force
if (Test-Path $PreservedConfig) {
    Copy-Item $PreservedConfig (Join-Path $PackageRoot "config.json") -Force
    Write-Host "Restored existing config.json — user settings survive rebuilds."
} else {
    Copy-Item "config.example.json" (Join-Path $PackageRoot "config.json") -Force
}
Copy-Item "README.md" (Join-Path $PackageRoot "README.md") -Force

$BrainSeed = $env:CHAT_NEXUS_BRAIN_SEED
if ($BrainSeed) {
    $BrainSeedPath = (Resolve-Path $BrainSeed -ErrorAction Stop).Path
    Write-Host "Validating creator-signed Nexus Brain seed..."
    & $Python -c "import json,pathlib,sys; p=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); assert p.get('format')=='chat-nexus-brain-locked'; a=p.get('creator_lock') or {}; b=p.get('brain') or {}; assert int(a.get('version',0))>=2; assert a.get('key_type')=='Ed25519'; assert a.get('public_key_pem'); assert a.get('public_key_sha256'); assert not a.get('encrypted_private_key_pem'); assert b.get('signature')" $BrainSeedPath
    if ($LASTEXITCODE -ne 0) { throw "CHAT_NEXUS_BRAIN_SEED is not a valid public read-only Ed25519 Nexus Brain export" }
    $BrainSeedTarget = Join-Path $PackageRoot "brain-seed"
    New-Item -ItemType Directory -Force -Path $BrainSeedTarget | Out-Null
    Copy-Item $BrainSeedPath (Join-Path $BrainSeedTarget "nexus-brain-locked.json") -Force
    Write-Host "Bundled creator-signed public Nexus Brain seed (private signing key not included)."
}

Write-Host "Bundling default ComfyUI API workflows..."
$WorkflowTarget = Join-Path $PackageRoot "workflows"
New-Item -ItemType Directory -Force -Path $WorkflowTarget | Out-Null
Copy-Item -Path (Join-Path $Root "workflows\*") -Destination $WorkflowTarget -Recurse -Force
$ExpectedImageWorkflows = @(
    "image\qwen\qwen-image-2.1-t2i-api.json",
    "image\qwen\qwen-image-2.1-edit-api.json",
    "image\qwen\qwen-image-2.1-inpaint-api.json",
    "image\qwen\qwen-image-2.1-background-removal-api.json",
    "image\flux\flux2-klein-4b-t2i-api.json",
    "image\flux\flux2-klein-4b-edit-api.json",
    "image\sdxl\juggernaut-x-v10-t2i-api.json"
)
foreach ($RelativeWorkflow in $ExpectedImageWorkflows) {
    if (-not (Test-Path (Join-Path $WorkflowTarget $RelativeWorkflow))) {
        throw "Required bundled image workflow is missing: $RelativeWorkflow"
    }
}

Write-Host "Cloning CI-verified Chat Nexus source into portable self-development workspace..."
git clone --no-hardlinks "$Root" (Join-Path $PackageRoot "Source")
if ($LASTEXITCODE -ne 0) { throw "Could not create bundled Source working copy" }
Push-Location (Join-Path $PackageRoot "Source")
git remote set-url origin "https://github.com/afterburn25/Coding_Agent.git"
Pop-Location
if (-not (Test-Path (Join-Path $PackageRoot "Source\.git\HEAD"))) { throw "Bundled Source workspace is missing Git metadata" }

Write-Host "Smoke testing native NexusCore.exe -> hidden backend integration..."
# The smoke test must write scratch state inside the package, not through
# the per-user junctions it would otherwise create and pollute.
$env:NEXUS_NO_STATE_REDIRECT = "1"
try {
    $Smoke = Start-Process -FilePath (Join-Path $PackageRoot "NexusCore.exe") -ArgumentList "--self-test" -WorkingDirectory $PackageRoot -PassThru -Wait
} finally {
    Remove-Item Env:NEXUS_NO_STATE_REDIRECT -ErrorAction SilentlyContinue
}
if ($Smoke.ExitCode -ne 0) { throw "Native NexusCore.exe self-test failed with exit code $($Smoke.ExitCode)" }

# The smoke test creates fresh runtime state under the package root. Strip it
# so deploying/updating never clobbers the installed app's user data
# (conversations, generated images, voice cache, etc.). Junctions (if any
# survived) are unlinked only — never traversed into the state root.
foreach ($runtimeDir in @("data", ".agent", "output", "logs")) {
    $p = Join-Path $PackageRoot $runtimeDir
    if (Test-Path $p) {
        $i = Get-Item $p -Force
        if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            cmd /c rmdir "$p" | Out-Null
        } else {
            Remove-Item -Recurse -Force $p -ErrorAction SilentlyContinue
        }
    }
}
if (Test-Path (Join-Path $PackageRoot "data")) { throw "Package still contains runtime data/ after cleanup" }

# Optional Authenticode signing. Unsigned binaries are what SmartScreen and
# AV heuristics flag on download — set NEXUS_CODESIGN_THUMBPRINT (a cert in
# the machine/user store) or NEXUS_CODESIGN_PFX (+ NEXUS_CODESIGN_PASSWORD)
# to sign the shipped executables. Without a cert this step is skipped.
$SignTool = $null
$KitsBin = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
if (Test-Path $KitsBin) {
    $SignTool = Get-ChildItem $KitsBin -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like "*x64*" } |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $SignTool) {
    $SignToolCmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($SignToolCmd) { $SignTool = $SignToolCmd.Source }
}
$SignCertArgs = @()
if ($env:NEXUS_CODESIGN_THUMBPRINT) {
    $SignCertArgs = @("/sha1", $env:NEXUS_CODESIGN_THUMBPRINT)
} elseif ($env:NEXUS_CODESIGN_PFX -and (Test-Path $env:NEXUS_CODESIGN_PFX)) {
    $SignCertArgs = @("/f", $env:NEXUS_CODESIGN_PFX)
    if ($env:NEXUS_CODESIGN_PASSWORD) { $SignCertArgs += @("/p", $env:NEXUS_CODESIGN_PASSWORD) }
}
if ($SignTool -and $SignCertArgs.Count -gt 0) {
    foreach ($Target in @(
        (Join-Path $PackageRoot "NexusCore.exe"),
        (Join-Path $PackageRoot "backend\ChatNexus.Backend.exe")
    )) {
        Write-Host "Signing $Target..."
        & $SignTool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /v @SignCertArgs $Target
        if ($LASTEXITCODE -ne 0) { throw "signtool failed for $Target" }
    }
} else {
    Write-Host "No code-signing certificate configured (NEXUS_CODESIGN_THUMBPRINT or NEXUS_CODESIGN_PFX) — binaries ship unsigned."
}

Write-Host "Native Nexus Core desktop package ready: $PackageRoot"
