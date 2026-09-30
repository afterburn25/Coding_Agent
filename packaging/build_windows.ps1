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

New-Item -ItemType Directory -Force -Path "packaging" | Out-Null
Remove-Item -Recurse -Force "build","dist",$RuntimeExtract -ErrorAction SilentlyContinue

Write-Host "Preparing Chat Nexus icon..."
& $Python -c "from PIL import Image; img=Image.open(r'web/assets/chat-nexus-emblem.png').convert('RGBA'); img.save(r'packaging/chat-nexus.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

Write-Host "Building native ChatNexus.exe..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name ChatNexus `
    --icon "packaging/chat-nexus.ico" `
    --add-data "web;web" `
    --collect-all webview `
    --hidden-import clr `
    --collect-submodules localcodeagent `
    "packaging/chat_nexus_entry.py"

if (-not (Test-Path "dist/ChatNexus/ChatNexus.exe")) {
    throw "PyInstaller did not produce dist/ChatNexus/ChatNexus.exe"
}

Write-Host "Downloading pinned llama.cpp Vulkan runtime $LlamaTag..."
Invoke-WebRequest -Uri $LlamaUrl -OutFile $RuntimeArchive
$ActualHash = (Get-FileHash -Algorithm SHA256 $RuntimeArchive).Hash.ToLowerInvariant()
if ($ActualHash -ne $LlamaSha256) {
    throw "llama.cpp archive checksum mismatch. Expected $LlamaSha256, got $ActualHash"
}
Expand-Archive -Path $RuntimeArchive -DestinationPath $RuntimeExtract -Force
$Server = Get-ChildItem -Path $RuntimeExtract -Recurse -Filter "llama-server.exe" | Select-Object -First 1
if (-not $Server) {
    throw "Pinned llama.cpp archive did not contain llama-server.exe"
}
$RuntimeTarget = Join-Path $Root "dist\ChatNexus\runtime\llama"
New-Item -ItemType Directory -Force -Path $RuntimeTarget | Out-Null
Copy-Item -Path (Join-Path $Server.Directory.FullName "*") -Destination $RuntimeTarget -Recurse -Force

Copy-Item "config.example.json" "dist/ChatNexus/config.example.json" -Force
Copy-Item "README.md" "dist/ChatNexus/README.md" -Force

Write-Host "Cloning CI-verified Chat Nexus source into the portable dogfood workspace..."
git clone --no-hardlinks "$Root" "dist/ChatNexus/Source"
if ($LASTEXITCODE -ne 0) { throw "Could not create bundled Source working copy" }
Push-Location "dist/ChatNexus/Source"
git remote set-url origin "https://github.com/afterburn25/Coding_Agent.git"
Pop-Location
Write-Host "Smoke testing packaged backend and embedded web UI..."
& "dist/ChatNexus/ChatNexus.exe" --smoke-test --workspace "$Root" --config "$Root/config.example.json"
if ($LASTEXITCODE -ne 0) {
    throw "Packaged ChatNexus.exe smoke test failed with exit code $LASTEXITCODE"
}

Write-Host "Native Chat Nexus desktop build ready: dist\ChatNexus\ChatNexus.exe"
