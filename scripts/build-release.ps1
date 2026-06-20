param(
    [switch]$Clean,
    [switch]$SkipTests,
    [switch]$SkipInstall,
    [switch]$SkipVenv
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistDir = Join-Path $ProjectRoot "dist"
$SrcVersionFile = Join-Path $ProjectRoot "src\foggy_runtime_cli\__init__.py"
$Pyproject = Join-Path $ProjectRoot "pyproject.toml"
$Manifest = Join-Path $DistDir "release-manifest.json"
$Checksums = Join-Path $DistDir "SHA256SUMS"
$ReleaseVenv = Join-Path $ProjectRoot ".release-venv"
$PythonExe = "python"

function Read-VersionFromPyproject {
    $line = Get-Content -Path $Pyproject | Where-Object { $_ -match '^version\s*=\s*"' } | Select-Object -First 1
    if (-not $line) {
        throw "Could not find version in pyproject.toml"
    }
    return ($line -replace '^version\s*=\s*"', '' -replace '"\s*$', '')
}

function Read-VersionFromPackage {
    $line = Get-Content -Path $SrcVersionFile | Where-Object { $_ -match '^__version__\s*=\s*"' } | Select-Object -First 1
    if (-not $line) {
        throw "Could not find __version__ in $SrcVersionFile"
    }
    return ($line -replace '^__version__\s*=\s*"', '' -replace '"\s*$', '')
}

Push-Location $ProjectRoot
try {
    $projectVersion = Read-VersionFromPyproject
    $packageVersion = Read-VersionFromPackage
    if ($projectVersion -ne $packageVersion) {
        throw "Version mismatch: pyproject.toml=$projectVersion, __init__.py=$packageVersion"
    }

    if ($Clean -and (Test-Path $DistDir)) {
        Remove-Item -LiteralPath $DistDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

    if (-not $SkipVenv) {
        if (-not (Test-Path $ReleaseVenv)) {
            python -m venv $ReleaseVenv
        }
        $PythonExe = Join-Path $ReleaseVenv "Scripts\python.exe"
    }

    if (-not $SkipInstall) {
        & $PythonExe -m pip install --upgrade pip build pytest
    }

    if (-not $SkipTests) {
        $env:PYTHONPATH = Join-Path $ProjectRoot "src"
        & $PythonExe -m pytest tests
    }

    & $PythonExe -m build --sdist --wheel

    $files = Get-ChildItem -Path $DistDir -File |
        Where-Object { $_.Name -match '\.(whl|tar\.gz)$' } |
        Sort-Object Name

    if (-not $files) {
        throw "No wheel or sdist artifacts were created in $DistDir"
    }

    $checksumLines = @()
    $artifactRows = @()
    foreach ($file in $files) {
        $hash = (Get-FileHash -Path $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $checksumLines += "$hash  $($file.Name)"
        $artifactRows += [ordered]@{
            file = $file.Name
            sha256 = $hash
            bytes = $file.Length
        }
    }

    $checksumLines | Out-File -FilePath $Checksums -Encoding ascii

    $manifestBody = [ordered]@{
        schemaVersion = "foggy-runtime-cli-release/v1"
        version = $projectVersion
        generatedAt = (Get-Date).ToString("o")
        artifacts = $artifactRows
        checksums = "SHA256SUMS"
    }
    $manifestBody | ConvertTo-Json -Depth 5 | Out-File -FilePath $Manifest -Encoding utf8

    Write-Host "Release package ready."
    Write-Host "Version: $projectVersion"
    Write-Host "Dist: $DistDir"
    foreach ($artifact in $artifactRows) {
        Write-Host "- $($artifact.file) sha256=$($artifact.sha256)"
    }
}
finally {
    Pop-Location
}
