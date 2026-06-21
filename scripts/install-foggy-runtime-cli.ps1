param(
    [string]$Version = "0.1.1",
    [string]$Repo = "foggy-projects/foggy-runtime-cli",
    [string]$Python = "python",
    [string]$DownloadDir = "",
    [string]$WheelPath = "",
    [string]$ChecksumsPath = "",
    [switch]$User
)

$ErrorActionPreference = "Stop"

function Resolve-TempDownloadDir {
    $root = Join-Path ([System.IO.Path]::GetTempPath()) "foggy-runtime-cli-install"
    $path = Join-Path $root $Version
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    return $path
}

function Download-File {
    param(
        [string]$Url,
        [string]$OutFile
    )
    Write-Host "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $OutFile
}

function Find-ExpectedHash {
    param(
        [string]$ChecksumsFile,
        [string]$AssetName
    )
    $line = Get-Content -Path $ChecksumsFile | Where-Object { $_ -match "\s+$([regex]::Escape($AssetName))$" } | Select-Object -First 1
    if (-not $line) {
        throw "Could not find $AssetName in $ChecksumsFile"
    }
    return ($line -split '\s+')[0].ToLowerInvariant()
}

$assetName = "foggy_runtime_cli-$Version-py3-none-any.whl"
if (-not $DownloadDir) {
    $DownloadDir = Resolve-TempDownloadDir
}
else {
    New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
}

if (-not $WheelPath) {
    $WheelPath = Join-Path $DownloadDir $assetName
    $releaseBase = "https://github.com/$Repo/releases/download/v$Version"
    Download-File -Url "$releaseBase/$assetName" -OutFile $WheelPath
}
else {
    $WheelPath = (Resolve-Path $WheelPath).Path
    $assetName = Split-Path -Path $WheelPath -Leaf
}

if (-not $ChecksumsPath) {
    $ChecksumsPath = Join-Path $DownloadDir "SHA256SUMS"
    $releaseBase = "https://github.com/$Repo/releases/download/v$Version"
    Download-File -Url "$releaseBase/SHA256SUMS" -OutFile $ChecksumsPath
}
else {
    $ChecksumsPath = (Resolve-Path $ChecksumsPath).Path
}

$expectedHash = Find-ExpectedHash -ChecksumsFile $ChecksumsPath -AssetName $assetName
$actualHash = (Get-FileHash -Path $WheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "SHA256 mismatch for $assetName. expected=$expectedHash actual=$actualHash"
}

Write-Host "SHA256 verified: $assetName"

$pipArgs = @("-m", "pip", "install", "--upgrade", $WheelPath)
if ($User) {
    $pipArgs += "--user"
}
& $Python @pipArgs

& $Python -m foggy_runtime_cli.main --help | Select-Object -First 12
Write-Host "foggy-runtime-cli $Version installed."
