param(
    [string]$Python = "python",
    [string]$LlamaTag = "b11278"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

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
$DesktopIcon = Join-Path $Root "desktop\ChatNexus.Desktop\chat-nexus.ico"

Remove-Item -Recurse -Force "build","dist",$RuntimeExtract -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "build","dist" | Out-Null

Write-Host "Preparing Chat Nexus Windows icon..."
& $Python -c "from PIL import Image; img=Image.open(r'web/assets/chat-nexus-emblem.png').convert('RGBA'); img.save(r'desktop/ChatNexus.Desktop/chat-nexus.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $DesktopIcon)) { throw "Could not create Chat Nexus application icon" }

Write-Host "Building hidden Python agent backend..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --console `
    --name "ChatNexus.Backend" `
    --distpath $BackendDist `
    --workpath $BackendWork `
    --specpath "build" `
    --add-data "$Root\web;web" `
    --add-data "$Root\tools;tools" `
    --collect-submodules localcodeagent `
    --collect-submodules py7zr `
    --hidden-import pybcj --hidden-import pyppmd --hidden-import pyzstd `
    --hidden-import brotli --hidden-import Brotli --hidden-import inflate64 `
    --hidden-import multivolumefile --hidden-import Cryptodome `
    "packaging/chat_nexus_backend_entry.py"
if ($LASTEXITCODE -ne 0) { throw "Chat Nexus backend PyInstaller build failed" }
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
if ($LASTEXITCODE -ne 0) { throw "Native Chat Nexus desktop publish failed" }
if (-not (Test-Path (Join-Path $DesktopPublish "ChatNexus.exe"))) { throw "Native ChatNexus.exe was not produced" }

New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
Copy-Item -Path (Join-Path $DesktopPublish "*") -Destination $PackageRoot -Recurse -Force
Copy-Item $DesktopIcon (Join-Path $PackageRoot "chat-nexus.ico") -Force

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

Copy-Item "config.example.json" (Join-Path $PackageRoot "config.example.json") -Force
Copy-Item "config.example.json" (Join-Path $PackageRoot "config.json") -Force
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
    "image\flux\flux2-klein-4b-edit-api.json"
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

Write-Host "Smoke testing native ChatNexus.exe -> hidden backend integration..."
$Smoke = Start-Process -FilePath (Join-Path $PackageRoot "ChatNexus.exe") -ArgumentList "--self-test" -WorkingDirectory $PackageRoot -PassThru -Wait
if ($Smoke.ExitCode -ne 0) { throw "Native ChatNexus.exe self-test failed with exit code $($Smoke.ExitCode)" }

Write-Host "Native Chat Nexus desktop package ready: $PackageRoot"
