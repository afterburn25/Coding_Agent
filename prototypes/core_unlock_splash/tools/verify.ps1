param([switch]$Native)
$ErrorActionPreference = 'Stop'
$prototypeRoot = Split-Path -Parent $PSScriptRoot
Push-Location $prototypeRoot
try {
    & node --test 'tests/*.test.mjs'
    if ($LASTEXITCODE -ne 0) { throw 'Timeline/audio tests failed' }
    & python -m unittest discover -s tests -p 'test_*.py'
    if ($LASTEXITCODE -ne 0) { throw 'Asset tests failed' }
    & dotnet build CoreUnlockSplash.csproj -c Release --nologo
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
    if ($Native) {
        $assembly = Join-Path $prototypeRoot 'bin/Release/net8.0-windows10.0.17763.0/NexusCore.SplashPreview.dll'
        $runRoot = Join-Path $prototypeRoot ('verification-local/' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))
        & dotnet $assembly --verify (Join-Path $runRoot 'normal')
        if ($LASTEXITCODE -ne 0) { throw 'Normal playback failed' }
        & dotnet $assembly --silent --verify (Join-Path $runRoot 'silent')
        if ($LASTEXITCODE -ne 0) { throw 'Silent playback failed' }
        & dotnet $assembly --static --verify (Join-Path $runRoot 'static')
        if ($LASTEXITCODE -ne 0) { throw 'Static fallback failed' }
        # Create a disposable distribution with a missing manifest. No source deletion.
        $faultCopy = Join-Path $runRoot 'missing-manifest-app'
        New-Item -ItemType Directory -Path $faultCopy -Force | Out-Null
        Get-ChildItem -LiteralPath (Split-Path -Parent $assembly) |
            Where-Object { $_.Name -ne 'animation_manifest.json' } |
            ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $faultCopy -Recurse }
        & dotnet (Join-Path $faultCopy 'NexusCore.SplashPreview.dll') --verify (Join-Path $runRoot 'missing-manifest-result')
        if ($LASTEXITCODE -ne 1) { throw 'Missing manifest did not report expected fallback' }
        $fallback = Get-Content (Join-Path $runRoot 'missing-manifest-result/fallback.json') -Raw | ConvertFrom-Json
        if (-not $fallback.staticFallback) { throw 'Missing manifest did not show static fallback' }
        Write-Output "Native verification passed. Reports: $runRoot"
    }
} finally { Pop-Location }
