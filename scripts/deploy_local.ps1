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

# Preserve uncommitted Source work — dist ships a fresh clone and /MIR
# purges anything not in it, which has destroyed live mission output
# (observed: a deliverable file wiped mid-deploy). Snapshot modified +
# untracked files first, restore them after the mirror.
$srcRepo = Join-Path $Dest "Source"
$backup = $null
$dirtyFiles = @()
if (Test-Path (Join-Path $srcRepo ".git")) {
    $porcelain = & git -C $srcRepo ls-files -mo --exclude-standard -z 2>$null
    if ($porcelain) { $dirtyFiles = @($porcelain -split "`0" | Where-Object { $_ }) }
}
if ($dirtyFiles.Count -gt 0) {
    $backup = Join-Path $env:TEMP ("nexus-source-backup-" + [guid]::NewGuid().ToString("N"))
    Write-Host "Preserving $($dirtyFiles.Count) uncommitted Source file(s) ..."
    foreach ($f in $dirtyFiles) {
        $from = Join-Path $srcRepo $f
        $to = Join-Path $backup $f
        if (Test-Path $from) {
            New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
            Copy-Item $from $to -Force
        }
    }
}

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

if ($backup -and (Test-Path $backup)) {
    robocopy $backup $srcRepo /E /IS /NFL /NDL /NP | Out-Null
    Remove-Item $backup -Recurse -Force
    Write-Host "Restored $($dirtyFiles.Count) uncommitted Source file(s)."
}
Write-Host "Deployed to $Dest (robocopy rc=$rc)."

# Restart the app if it was running before the deploy.
if ($running) {
    Write-Host "Relaunching NexusCore.exe ..."
    Start-Process (Join-Path $Dest "NexusCore.exe")
}
