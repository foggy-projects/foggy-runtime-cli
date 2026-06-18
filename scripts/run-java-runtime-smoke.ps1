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
    [string]$ComposePayload = "",
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
    $validationMessage = ""
    if ($result -eq "passed") {
        $validationMessage = Test-CliJsonOutput -Name $Name -OutputPath $outFile
        if ($validationMessage) {
            $result = "failed"
        }
    }
    [pscustomobject]@{
        Command = $Name
        ExitCode = $exitCode
        Result = $result
        Output = $outFile
        Error = $errFile
        Arguments = $Arguments
        ValidationMessage = $validationMessage
    }
}

function Test-CliJsonOutput {
    param(
        [string]$Name,
        [string]$OutputPath
    )

    try {
        $body = Get-Content $OutputPath -Raw | ConvertFrom-Json
    }
    catch {
        return "Output is not valid JSON: $($_.Exception.Message)"
    }

    if ($body.success -ne $true) {
        return "Runtime envelope success is not true."
    }

    switch ($Name) {
        "ready-capabilities" {
            return Test-CapabilitiesOutput -Body $body
        }
        "capabilities" {
            return Test-CapabilitiesOutput -Body $body
        }
        "models-refresh" {
            if ([int]$body.data.failedCount -ne 0) {
                return "models.refresh failedCount is $($body.data.failedCount)."
            }
            if ([int]$body.data.loadedCount -lt 1) {
                return "models.refresh loadedCount is $($body.data.loadedCount)."
            }
        }
        "models-validate" {
            if ($body.data.valid -ne $true) {
                return "models.validate data.valid is not true."
            }
            if ([int]$body.data.totalFiles -lt 2) {
                return "models.validate totalFiles is $($body.data.totalFiles)."
            }
            if ([int]$body.data.invalidFiles -ne 0) {
                return "models.validate invalidFiles is $($body.data.invalidFiles)."
            }
        }
        "models-describe" {
            if (-not $body.data.data.models.$ModelName) {
                return "models.describe does not include model $ModelName."
            }
            if (-not $body.data.data.fields -or $body.data.data.fields.PSObject.Properties.Count -lt 1) {
                return "models.describe has no fields."
            }
        }
        "query-validate" {
            if ($body.diagnostics.warnings.Count -gt 0) {
                return "query.validate returned diagnostics warnings."
            }
        }
        "query-execute" {
            if (-not $body.data.items -or $body.data.items.Count -lt 1) {
                return "query.execute returned no items."
            }
        }
        "compose-validate" {
            return Test-ComposeOutput -Body $body -Mode "validate"
        }
        "compose-preview" {
            $message = Test-ComposeOutput -Body $body -Mode "preview"
            if ($message) {
                return $message
            }
            $json = $body.data | ConvertTo-Json -Depth 20 -Compress
            if ($json -notmatch "sql") {
                return "compose.preview did not expose SQL evidence."
            }
        }
        "compose-execute" {
            return Test-ComposeOutput -Body $body -Mode "execute"
        }
        "fsscript-run" {
            if ($body.data.valid -ne $true) {
                return "fsscript.run data.valid is not true."
            }
            if ($body.data.scriptKind -ne "fsscript") {
                return "fsscript.run scriptKind is $($body.data.scriptKind)."
            }
            if ($body.data.mode -ne "execute") {
                return "fsscript.run mode is $($body.data.mode)."
            }
            if ([int]$body.data.value -ne 3) {
                return "fsscript.run value is $($body.data.value), expected 3."
            }
        }
        "fsscript-cte-bridge-preview" {
            if ($body.data.valid -ne $true) {
                return "fsscript CTE bridge data.valid is not true."
            }
            if ($body.data.scriptKind -ne "fsscript") {
                return "fsscript CTE bridge scriptKind is $($body.data.scriptKind)."
            }
            if ($body.data.value.scriptKind -ne "compose") {
                return "fsscript CTE bridge nested scriptKind is $($body.data.value.scriptKind)."
            }
            if ($body.data.value.mode -ne "preview") {
                return "fsscript CTE bridge nested mode is $($body.data.value.mode)."
            }
            $json = $body.data.value | ConvertTo-Json -Depth 20 -Compress
            if ($json -notmatch "sql") {
                return "fsscript CTE bridge preview did not expose nested SQL evidence."
            }
        }
        "tables-inspect" {
            if ($body.data.table -ne $TableName) {
                return "tables.inspect returned table $($body.data.table), expected $TableName."
            }
            if (-not $body.data.columns -or $body.data.columns.Count -lt 1) {
                return "tables.inspect returned no columns."
            }
        }
    }

    return ""
}

function Test-ComposeOutput {
    param(
        [object]$Body,
        [string]$Mode
    )

    if ($Body.data.valid -ne $true) {
        return "compose.$Mode data.valid is not true."
    }
    if ($Body.data.scriptKind -ne "compose") {
        return "compose.$Mode scriptKind is $($Body.data.scriptKind)."
    }
    if ($Body.data.mode -ne $Mode) {
        return "compose.$Mode mode is $($Body.data.mode)."
    }
    return ""
}

function Test-CapabilitiesOutput {
    param([object]$Body)

    if ($Body.engine -ne "java") {
        return "capabilities engine is $($Body.engine), expected java."
    }
    if ($Body.runtimeApiVersion -ne "foggy-runtime-api/v1") {
        return "capabilities runtimeApiVersion is $($Body.runtimeApiVersion)."
    }
    if ($Body.data.schemaVersion -ne "2026-06-06") {
        return "capabilities schemaVersion is $($Body.data.schemaVersion)."
    }
    if ($Body.data.securityMode -ne "none-dev-test-only") {
        return "capabilities securityMode is $($Body.data.securityMode)."
    }
    foreach ($capability in @(
        "models.refresh",
        "models.validate",
        "models.describe",
        "query.validate",
        "query.execute",
        "tables.inspect",
        "compose.validate",
        "compose.preview",
        "compose.execute",
        "fsscript.execute",
        "fsscript.cteBridge"
    )) {
        $value = $Body.data.capabilities.PSObject.Properties[$capability].Value
        if ($value -ne "supported") {
            return "capability $capability is $value."
        }
    }

    return ""
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
if (-not $ComposePayload) {
    $ComposePayload = Resolve-DefaultPath -Base $RepoRoot -Relative "docs\v4.1\contracts\runtime-api-v1\fixtures\compose-fact-order-request.json"
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
    if (-not (Test-Path $ComposePayload)) {
        throw "ComposePayload not found: $ComposePayload"
    }

    $env:PYTHONPATH = if ($oldPythonPath) { "$CliSrc;$oldPythonPath" } else { $CliSrc }
    $composeFixture = Get-Content $ComposePayload -Raw | ConvertFrom-Json
    $composeScript = [string]$composeFixture.script
    if (-not $composeScript) {
        throw "ComposePayload does not contain script: $ComposePayload"
    }
    $composeScriptJson = $composeScript | ConvertTo-Json -Compress
    $fsscriptCteBridgeScript = "return foggy.cte.preview({script: $composeScriptJson});"

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
    $results += Invoke-CliCommand -Name "models-validate" -Arguments @("models", "validate", "--models-dir", $ModelsDir)
    $results += Invoke-CliCommand -Name "models-refresh" -Arguments @("models", "refresh", "--model", $ModelName)
    $results += Invoke-CliCommand -Name "models-describe" -Arguments @("models", "describe", $ModelName)
    $results += Invoke-CliCommand -Name "query-validate" -Arguments @("query", "validate", $ModelName, "--payload", $QueryPayload)
    $results += Invoke-CliCommand -Name "query-execute" -Arguments @("query", "execute", $ModelName, "--payload", $QueryPayload)
    $results += Invoke-CliCommand -Name "compose-validate" -Arguments @("compose", "validate", "--script-text", $composeScript)
    $results += Invoke-CliCommand -Name "compose-preview" -Arguments @("compose", "preview", "--script-text", $composeScript)
    $results += Invoke-CliCommand -Name "compose-execute" -Arguments @("compose", "execute", "--script-text", $composeScript)
    $results += Invoke-CliCommand -Name "fsscript-run" -Arguments @("fsscript", "run", "--script-text", "return 1 + 2;")
    $results += Invoke-CliCommand -Name "fsscript-cte-bridge-preview" -Arguments @("fsscript", "run", "--script-text", $fsscriptCteBridgeScript, "--enable-cte-bridge")
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
        composePayload = $ComposePayload
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
