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
#   .nexus                — live mission lane worktrees under Source\
#                           (observed: /MIR purged all 3 executing lanes'
#                           checkouts mid-deploy — committed branch work
#                           survived but uncommitted lane work was lost)
#   .repair-worktrees     — self-repair patch worktrees, same reason
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
# Its helper children (cmd/python running localcodeagent.selftest,
# unittest discover, etc.) run executables OUTSIDE $Dest so the path
# filter misses them — but they keep the deployed tree's DLLs open and
# robocopy then fails mid-mirror (observed: dbghelp/msvcp140 locks,
# rc=11). Capture them BEFORE killing the parents, since once orphaned
# they can only be identified by their command line.
$helperIds = @()
if ($running) {
    $parentIds = @($running.Id)
    $helperIds = @(Get-CimInstance Win32_Process |
        Where-Object { $parentIds -contains $_.ParentProcessId } |
        ForEach-Object { $_.ProcessId })
    # Grandchildren too (selftest spawns unittest discover under itself).
    $helperIds += @(Get-CimInstance Win32_Process |
        Where-Object { $helperIds -contains $_.ParentProcessId } |
        ForEach-Object { $_.ProcessId })
}
if ($running) {
    Write-Host "Stopping running Nexus processes under $Dest ..."
    $running | Stop-Process -Force
}
if ($helperIds) {
    $helperIds | ForEach-Object {
        Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
}
# Orphans from an earlier kill — parent already dead, still holding
# locks. The dead-parent check protects a developer's own `unittest
# discover` runs, which always have a live shell parent.
$nexusOrphans = @(Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -in 'python.exe', 'pythonw.exe', 'cmd.exe') -and
        ($_.CommandLine -match 'localcodeagent\.selftest|unittest discover') -and
        -not (Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue)
    })
if ($nexusOrphans) {
    Write-Host "Reaping $($nexusOrphans.Count) orphaned selftest helper(s) ..."
    $nexusOrphans | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
if ($running -or $helperIds -or $nexusOrphans) {
    Start-Sleep -Seconds 3
}

$excludeDirs = @("data", "output", "runtime", "models", "tools",
                 ".git", ".agent", ".nexus", ".repair-worktrees")
# /XJ is mandatory here: data, output and .agent under the install
# root are junctions into %LOCALAPPDATA%\NexusCore. Junction points
# must never be traversed, copied or deleted by the mirror —
# (observed 2026-10-09: the entire live state at the per-user root —
# missions, profiles, social, secrets.vault — was empty after a
# deploy+restart; root cause not conclusively isolated, so /XJ is
# defense-in-depth alongside the /XD name exclusions).
$args = @($Source, $Dest, "/MIR", "/XJ", "/XD") + $excludeDirs +
        @("/XF", "config.json", "/R:2", "/W:1", "/NFL", "/NDL", "/NP")
robocopy @args | Select-Object -Last 12
$rc = $LASTEXITCODE

# Restore preserved uncommitted work BEFORE the failure check — a
# partially-failed mirror must never strand the snapshot in %TEMP%
# (observed: rc=11 throw skipped this restore, leaving 40 files
# orphaned in a temp dir while Source had been reset to the clone).
if ($backup -and (Test-Path $backup)) {
    robocopy $backup $srcRepo /E /IS /NFL /NDL /NP | Out-Null
    Remove-Item $backup -Recurse -Force
    Write-Host "Restored $($dirtyFiles.Count) uncommitted Source file(s)."
}

if ($rc -ge 8) {
    # Observed: a locked exe mid-mirror threw here and left the app DOWN —
    # the next clean run saw nothing running and skipped relaunch too.
    if ($running) {
        Write-Host "Partial deploy — relaunching NexusCore.exe ..."
        Start-Process (Join-Path $Dest "NexusCore.exe")
    }
    throw "robocopy failed with exit code $rc"
}
Write-Host "Deployed to $Dest (robocopy rc=$rc)."

# Restart the app if it was running before the deploy.
if ($running) {
    Write-Host "Relaunching NexusCore.exe ..."
    Start-Process (Join-Path $Dest "NexusCore.exe")
}
