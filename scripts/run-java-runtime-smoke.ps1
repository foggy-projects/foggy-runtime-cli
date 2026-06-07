param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$JavaRoot = "",
    [string]$EvidenceDir = "",
    [int]$Port = 18066,
    [string]$Namespace = "default",
    [string]$ModelName = "FactOrderQueryModel",
    [string]$TableName = "fact_order",
    [string]$ModelsDir = "",
    [string]$QueryPayload = "",
    [int]$ReadyTimeoutSeconds = 90,
    [switch]$Build,
    [switch]$ReuseExisting,
    [switch]$NoStop
)

$ErrorActionPreference = "Stop"

function Resolve-DefaultPath {
    param(
        [string]$Base,
        [string]$Relative
    )
    return (Join-Path $Base $Relative)
}

function Test-PortOpen {
    param([int]$TargetPort)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(500)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Get-LauncherJar {
    param([string]$LauncherTarget)

    $jar = Get-ChildItem -Path $LauncherTarget -Filter "foggy-mcp-launcher-*.jar" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike "*-sources.jar" } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $jar) {
        return $null
    }
    return $jar.FullName
}

function Invoke-Checked {
    param(
        [string]$Name,
        [scriptblock]$Action
    )

    Write-Host "==> $Name"
    & $Action
    if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

function Invoke-CliCommand {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [int[]]$ExpectedExitCodes = @(0)
    )

    $outFile = Join-Path $EvidenceDir "$Name.json"
    $errFile = Join-Path $EvidenceDir "$Name.err.log"
    $baseUrl = "http://127.0.0.1:$Port"
    $pythonArgs = @(
        "-m", "foggy_runtime_cli.main",
        "--base-url", $baseUrl,
        "--namespace", $Namespace
    ) + $Arguments

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python @pythonArgs 1> $outFile 2> $errFile
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $result = if ($ExpectedExitCodes -contains $exitCode) { "passed" } else { "failed" }
    [pscustomobject]@{
        Command = $Name
        ExitCode = $exitCode
        Result = $result
        Output = $outFile
        Error = $errFile
        Arguments = $Arguments
    }
}

function Wait-RuntimeReady {
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($startedProcess -and $startedProcess.HasExited) {
            throw "Java runtime exited before readiness. See $serverErr"
        }

        $probe = Invoke-CliCommand -Name "ready-capabilities" -Arguments @("capabilities")
        if ($probe.ExitCode -eq 0) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "Runtime API did not become ready within $ReadyTimeoutSeconds seconds"
}

if (-not $JavaRoot) {
    $JavaRoot = Resolve-DefaultPath -Base $RepoRoot -Relative "foggy-data-mcp-bridge-wt-dev-compose"
}
if (-not $EvidenceDir) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $EvidenceDir = Resolve-DefaultPath -Base $RepoRoot -Relative ".codex-tmp\runtime-cli-java-smoke\$stamp"
}
if (-not $ModelsDir) {
    $ModelsDir = Resolve-DefaultPath -Base $RepoRoot -Relative "docs\v4.1\contracts\runtime-api-v1\model-fixtures\minimal-fact-order"
}
if (-not $QueryPayload) {
    $QueryPayload = Resolve-DefaultPath -Base $RepoRoot -Relative "docs\v4.1\contracts\runtime-api-v1\fixtures\query-fact-order-valid.json"
}

$CliSrc = Resolve-DefaultPath -Base $RepoRoot -Relative "foggy-runtime-cli\src"
$launcherTarget = Resolve-DefaultPath -Base $JavaRoot -Relative "foggy-mcp-launcher\target"
$sqlitePath = Join-Path $EvidenceDir "foggy_mcp_lite.db"
$serverOut = Join-Path $EvidenceDir "server.out.log"
$serverErr = Join-Path $EvidenceDir "server.err.log"
$summaryPath = Join-Path $EvidenceDir "summary.json"
$startedProcess = $null
$oldPythonPath = $env:PYTHONPATH

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

try {
    if (-not (Test-Path $CliSrc)) {
        throw "CLI src path not found: $CliSrc"
    }
    if (-not (Test-Path $JavaRoot)) {
        throw "Java root not found: $JavaRoot"
    }
    if (-not (Test-Path $ModelsDir)) {
        throw "ModelsDir not found: $ModelsDir"
    }
    if (-not (Test-Path $QueryPayload)) {
        throw "QueryPayload not found: $QueryPayload"
    }

    $env:PYTHONPATH = if ($oldPythonPath) { "$CliSrc;$oldPythonPath" } else { $CliSrc }

    if ($Build) {
        Push-Location $JavaRoot
        try {
            Invoke-Checked -Name "Build foggy-mcp-launcher with runtime-api profile" -Action {
                & mvn -pl foggy-mcp-launcher -am -Pruntime-api -DskipTests package
            }
        }
        finally {
            Pop-Location
        }
    }

    $jarPath = Get-LauncherJar -LauncherTarget $launcherTarget
    if (-not $jarPath) {
        Push-Location $JavaRoot
        try {
            Invoke-Checked -Name "Build missing foggy-mcp-launcher jar" -Action {
                & mvn -pl foggy-mcp-launcher -am -Pruntime-api -DskipTests package
            }
        }
        finally {
            Pop-Location
        }
        $jarPath = Get-LauncherJar -LauncherTarget $launcherTarget
    }
    if (-not $jarPath) {
        throw "Launcher jar not found under $launcherTarget"
    }

    if (Test-PortOpen -TargetPort $Port) {
        if (-not $ReuseExisting) {
            throw "Port $Port is already open. Use -ReuseExisting or choose another -Port."
        }
        Write-Host "Reusing existing runtime on port $Port"
    }
    else {
        $javaArgs = @(
            "-jar", $jarPath,
            "--server.port=$Port",
            "--spring.profiles.active=lite",
            "--foggy.runtime-api.enabled=true",
            "--foggy.data-viewer.enabled=false",
            "--foggy.mcp.audit.enabled=false",
            "--spring.datasource.url=jdbc:sqlite:$sqlitePath",
            "--spring.ai.openai.api-key=sk-runtime-smoke",
            "--spring.ai.openai.base-url=http://127.0.0.1:9",
            "--spring.ai.openai.chat.options.model=runtime-smoke"
        )
        $startedProcess = Start-Process -FilePath "java" `
            -ArgumentList $javaArgs `
            -WorkingDirectory $JavaRoot `
            -RedirectStandardOutput $serverOut `
            -RedirectStandardError $serverErr `
            -PassThru `
            -WindowStyle Hidden
        $startedProcess.Id | Out-File -FilePath (Join-Path $EvidenceDir "server.pid") -Encoding utf8
        Write-Host "Started Java runtime pid=$($startedProcess.Id) port=$Port"
    }

    Wait-RuntimeReady

    $results = @()
    $results += Invoke-CliCommand -Name "capabilities" -Arguments @("capabilities")
    $results += Invoke-CliCommand -Name "models-list" -Arguments @("models", "list")
    $results += Invoke-CliCommand -Name "models-refresh" -Arguments @("models", "refresh", "--model", $ModelName)
    $results += Invoke-CliCommand -Name "models-validate" -Arguments @("models", "validate", "--models-dir", $ModelsDir)
    $results += Invoke-CliCommand -Name "models-describe" -Arguments @("models", "describe", $ModelName)
    $results += Invoke-CliCommand -Name "query-validate" -Arguments @("query", "validate", $ModelName, "--payload", $QueryPayload)
    $results += Invoke-CliCommand -Name "query-execute" -Arguments @("query", "execute", $ModelName, "--payload", $QueryPayload)
    $results += Invoke-CliCommand -Name "tables-inspect" -Arguments @("tables", "inspect", "--table", $TableName, "--include-indexes")

    $summary = [pscustomobject]@{
        generatedAt = (Get-Date).ToString("o")
        baseUrl = "http://127.0.0.1:$Port"
        namespace = $Namespace
        javaRoot = $JavaRoot
        jarPath = $jarPath
        evidenceDir = $EvidenceDir
        modelName = $ModelName
        tableName = $TableName
        modelsDir = $ModelsDir
        queryPayload = $QueryPayload
        results = $results
    }
    $summary | ConvertTo-Json -Depth 8 | Out-File -FilePath $summaryPath -Encoding utf8

    $failed = @($results | Where-Object { $_.Result -ne "passed" })
    if ($failed.Count -gt 0) {
        Write-Error "Runtime CLI Java smoke failed. See $summaryPath"
        exit 1
    }

    Write-Host "Runtime CLI Java smoke passed. Evidence: $EvidenceDir"
    exit 0
}
finally {
    $env:PYTHONPATH = $oldPythonPath
    if ($startedProcess -and -not $NoStop) {
        if (-not $startedProcess.HasExited) {
            Stop-Process -Id $startedProcess.Id -Force
            Write-Host "Stopped Java runtime pid=$($startedProcess.Id)"
        }
    }
}
