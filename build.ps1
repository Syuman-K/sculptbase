# Build an installable extension ZIP for SculptBase.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build.ps1
#   powershell -ExecutionPolicy Bypass -File .\build.ps1 -SkipTests
#
# By default it runs the headless tests first and aborts if they fail.
# Point $env:BLENDER at blender.exe if it is not auto-detected.

param([switch]$SkipTests)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"

function Find-Blender {
    if ($env:BLENDER -and (Test-Path $env:BLENDER)) { return $env:BLENDER }
    $c = Get-Command blender.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $guess = Get-ChildItem "C:\Program Files\Blender Foundation" -Recurse `
        -Filter blender.exe -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($guess) { return $guess.FullName }
    return $null
}

$blender = Find-Blender
if (-not $blender) {
    throw "Blender not found. Set `$env:BLENDER to blender.exe."
}

if (-not $SkipTests) {
    Write-Host "Running headless tests with: $blender"
    & $blender -b --factory-startup --python (Join-Path $root "_test\test_sculptbase.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Tests FAILED (exit $LASTEXITCODE). Build aborted. " +
              "Use -SkipTests to override."
    }
    Write-Host "Tests passed."
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null
& $blender --command extension build --source-dir $root --output-dir $dist
if ($LASTEXITCODE -ne 0) { throw "Extension build failed (exit $LASTEXITCODE)." }

Write-Host "Built ZIP into: $dist"
