# deploy_local.ps1 — mirror dist\ChatNexus into the installed workstation.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_local.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_local.ps1 -Dest D:\Nexus_Core
#
# IMPORTANT — robocopy /XD semantics:
#   Bare directory NAMES protect the whole subtree from /MIR purge.
#   Absolute paths do NOT — the purge phase still treats excluded-path
#   children as *EXTRA and deletes them (observed: runtime\voice\chatterbox
#   and tools\ wiped mid-deploy even though "D:\Nexus_Core\runtime" was
#   listed under Exc Dirs). Keep these as bare names.
#
# Protected from mirroring (persistent on the target, not in the bundle):
#   data, output          — live user state / outputs
#   runtime, models       — provisioned runtimes (llama, chatterbox) + model weights
#   tools                 — ComfyUI portable, InvokeAI, other tool payloads
#   .git                  — deployed Source checkout metadata
#   .agent                — durable task ledger + checkpoints under Source\
#                           (dist ships a fresh clone; wiping it mid-deploy
#                           strands in-flight tasks and parked missions)
#   config.json           — live user configuration (file-level exclusion)
param(
    [string]$Source = (Join-Path $PSScriptRoot "..\dist\ChatNexus"),
    [string]$Dest = "D:\Nexus_Core"
)

$Source = (Resolve-Path $Source).Path
if (-not (Test-Path $Dest)) { throw "Deploy target '$Dest' does not exist" }

# The running app locks its binaries — refuse to half-deploy.
$running = Get-Process | Where-Object { $_.Path -like "$Dest\*" }
if ($running) {
    Write-Host "Stopping running Nexus processes under $Dest ..."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 3
}

$excludeDirs = @("data", "output", "runtime", "models", "tools",
                 ".git", ".agent")
$args = @($Source, $Dest, "/MIR", "/XD") + $excludeDirs +
        @("/XF", "config.json", "/R:2", "/W:1", "/NFL", "/NDL", "/NP")
robocopy @args | Select-Object -Last 12
$rc = $LASTEXITCODE

if ($rc -ge 8) { throw "robocopy failed with exit code $rc" }
Write-Host "Deployed to $Dest (robocopy rc=$rc)."

# Restart the app if it was running before the deploy.
if ($running) {
    Write-Host "Relaunching NexusCore.exe ..."
    Start-Process (Join-Path $Dest "NexusCore.exe")
}
