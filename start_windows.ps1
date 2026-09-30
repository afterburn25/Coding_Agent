param(
    [string]$Workspace = (Get-Location).Path,
    [int]$Port = 8765
)
Set-Location $PSScriptRoot
if (-not (Test-Path "config.json")) {
    Copy-Item "config.example.json" "config.json"
}
Write-Host "Starting Local Code Agent..."
Write-Host "Workspace: $Workspace"
Write-Host "UI: http://127.0.0.1:$Port"
python -m localcodeagent --workspace $Workspace --config config.json --port $Port
