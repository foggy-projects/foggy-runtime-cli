param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$EvidenceDir = "",
    [int]$Port = 18066,
    [string]$Namespace = "salesdrop",
    [string]$LauncherJar = "",
    [string]$SkillValidator = "",
    [switch]$SkipSkillValidation
)

$ErrorActionPreference = "Stop"

function Resolve-DefaultPath {
    param(
        [string]$Base,
        [string]$Relative
    )
    return Join-Path $Base $Relative
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

function Format-CommandLine {
    param(
        [string]$File,
        [string[]]$CommandArgs
    )

    $parts = @($File) + $CommandArgs
    return (($parts | ForEach-Object {
        $value = [string]$_
        if ($value -match '[\s`"$]') {
            '"' + ($value -replace '"', '\"') + '"'
        }
        else {
            $value
        }
    }) -join " ")
}

function Add-Check {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Message,
        [string]$Command = "",
        [int]$ExitCode = 0,
        [string]$Output = "",
        [string]$Error = ""
    )

    $script:Checks += [pscustomobject]@{
        name = $Name
        status = $Status
        message = $Message
        command = $Command
        exitCode = $ExitCode
        output = $Output
        error = $Error
    }
}

function Add-PathCheck {
    param(
        [string]$Name,
        [string]$Path,
        [bool]$Required = $true
    )

    if (Test-Path $Path) {
        Add-Check -Name $Name -Status "passed" -Message $Path
        return
    }

    $status = if ($Required) { "failed" } else { "warning" }
    Add-Check -Name $Name -Status $status -Message "Missing: $Path"
}

function Invoke-CheckCommand {
    param(
        [string]$Name,
        [string]$File,
        [string[]]$CommandArgs,
        [scriptblock]$Validator = $null
    )

    $safeName = ($Name -replace '[^A-Za-z0-9._-]+', '-').Trim("-")
    $outFile = Join-Path $EvidenceDir "$safeName.out.log"
    $errFile = Join-Path $EvidenceDir "$safeName.err.log"
    $commandLine = Format-CommandLine -File $File -CommandArgs $CommandArgs

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $File @CommandArgs 1> $outFile 2> $errFile
        $exitCode = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
    }
    catch {
        $exitCode = 1
        $_.Exception.Message | Out-File -FilePath $errFile -Encoding utf8
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $validationMessage = ""
    if ($exitCode -eq 0 -and $Validator) {
        try {
            $validationMessage = & $Validator $outFile
        }
        catch {
            $validationMessage = $_.Exception.Message
        }
    }

    $status = if ($exitCode -eq 0 -and -not $validationMessage) { "passed" } else { "failed" }
    $message = if ($validationMessage) { $validationMessage } elseif ($exitCode -eq 0) { "ok" } else { "exit $exitCode" }
    Add-Check -Name $Name -Status $status -Message $message -Command $commandLine -ExitCode $exitCode -Output $outFile -Error $errFile
}

function Test-DemoPlanJson {
    param([string]$OutputPath)

    try {
        $body = Get-Content -Path $OutputPath -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        return "demo plan output is not valid JSON: $($_.Exception.Message)"
    }
    if ($body.success -ne $true) {
        return "demo plan success is not true."
    }
    $commands = @($body.data.commands | ForEach-Object { $_.name })
    foreach ($required in @("start-runtime", "wait-ready", "capabilities", "models-validate", "query-execute")) {
        if ($commands -notcontains $required) {
            return "demo plan does not include command: $required"
        }
    }
    return ""
}

if (-not $EvidenceDir) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $EvidenceDir = Resolve-DefaultPath -Base $RepoRoot -Relative ".codex-tmp\foggy-ai-analysis-demo\preflight-$stamp"
}
if (-not $LauncherJar) {
    $LauncherJar = Resolve-DefaultPath -Base $RepoRoot -Relative "foggy-data-mcp-bridge-wt-dev-compose\foggy-mcp-launcher\target\foggy-mcp-launcher-9.1.0.beta.jar"
}
if (-not $SkillValidator) {
    $SkillValidator = Join-Path $env:USERPROFILE ".codex\skills\.system\skill-creator\scripts\quick_validate.py"
}

$SkillDir = Resolve-DefaultPath -Base $RepoRoot -Relative ".codex\skills\foggy-ai-analysis-demo"
$DemoDir = Join-Path $SkillDir "assets\sales-drop-demo"
$CliSrc = Resolve-DefaultPath -Base $RepoRoot -Relative "foggy-runtime-cli\src"
$ReportJson = Join-Path $EvidenceDir "preflight-report.json"
$ReportMarkdown = Join-Path $EvidenceDir "preflight-report.md"
$script:Checks = @()
$oldPythonPath = $env:PYTHONPATH

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

try {
    Add-PathCheck -Name "repo-root" -Path $RepoRoot
    Add-PathCheck -Name "skill-dir" -Path $SkillDir
    Add-PathCheck -Name "demo-schema" -Path (Join-Path $DemoDir "schema.sql")
    Add-PathCheck -Name "demo-data" -Path (Join-Path $DemoDir "data.sql")
    Add-PathCheck -Name "demo-models" -Path (Join-Path $DemoDir "models")
    Add-PathCheck -Name "question-bank" -Path (Join-Path $DemoDir "question-bank.json")
    Add-PathCheck -Name "cli-src" -Path $CliSrc
    Add-PathCheck -Name "launcher-jar" -Path $LauncherJar -Required $false

    if (Test-PortOpen -TargetPort $Port) {
        Add-Check -Name "port-available" -Status "warning" -Message "Port $Port is already open; choose another port or use an existing runtime intentionally."
    }
    else {
        Add-Check -Name "port-available" -Status "passed" -Message "Port $Port is available."
    }

    Invoke-CheckCommand -Name "java-version" -File "java" -CommandArgs @("-version")
    Invoke-CheckCommand -Name "python-version" -File "python" -CommandArgs @("--version")

    $env:PYTHONPATH = if ($oldPythonPath) { "$CliSrc;$oldPythonPath" } else { $CliSrc }
    Invoke-CheckCommand -Name "cli-help" -File "python" -CommandArgs @("-m", "foggy_runtime_cli.main", "--help")
    Invoke-CheckCommand -Name "demo-sales-drop-plan" -File "python" -CommandArgs @(
        "-m",
        "foggy_runtime_cli.main",
        "--namespace",
        $Namespace,
        "demo",
        "sales-drop",
        "plan",
        "--repo-root",
        $RepoRoot,
        "--port",
        "$Port",
        "--launcher-jar",
        $LauncherJar
    ) -Validator ${function:Test-DemoPlanJson}

    if ($SkipSkillValidation) {
        Add-Check -Name "skill-validation" -Status "warning" -Message "Skipped by -SkipSkillValidation."
    }
    elseif (Test-Path $SkillValidator) {
        Invoke-CheckCommand -Name "skill-validation" -File "python" -CommandArgs @($SkillValidator, $SkillDir)
    }
    else {
        Add-Check -Name "skill-validation" -Status "warning" -Message "Skill validator not found: $SkillValidator"
    }
}
finally {
    $env:PYTHONPATH = $oldPythonPath
}

$failed = @($script:Checks | Where-Object { $_.status -eq "failed" })
$warnings = @($script:Checks | Where-Object { $_.status -eq "warning" })
$status = if ($failed.Count -gt 0) { "failed" } elseif ($warnings.Count -gt 0) { "passed-with-warnings" } else { "passed" }

$report = [ordered]@{
    schemaVersion = "foggy-ai-analysis-demo-preflight/v1"
    generatedAt = (Get-Date).ToString("o")
    status = $status
    repoRoot = $RepoRoot
    evidenceDir = $EvidenceDir
    port = $Port
    namespace = $Namespace
    launcherJar = $LauncherJar
    checks = $script:Checks
    failedCount = $failed.Count
    warningCount = $warnings.Count
    notes = @(
        "This preflight does not start Java or call Runtime API endpoints.",
        "Production permission, auth, RBAC, audit, and governance remain deferred."
    )
}
$report | ConvertTo-Json -Depth 8 | Out-File -FilePath $ReportJson -Encoding utf8

$markdown = @(
    "# Foggy AI Analysis Demo Preflight",
    "",
    "- Status: $status",
    "- Repo root: $RepoRoot",
    "- Port: $Port",
    "- Namespace: $Namespace",
    "- Launcher JAR: $LauncherJar",
    "- Evidence: $EvidenceDir",
    "",
    "## Checks",
    ""
)
foreach ($check in $script:Checks) {
    $line = "- $($check.name): $($check.status) - $($check.message)"
    if ($check.command) {
        $line += " (`$($check.command)`)"
    }
    $markdown += $line
}
$markdown += @(
    "",
    "## Boundary",
    "",
    "This preflight is for trusted local dev/test onboarding. Production permission, auth, RBAC, audit, and governance are deferred."
)
$markdown | Out-File -FilePath $ReportMarkdown -Encoding utf8

Write-Host "Preflight status: $status"
Write-Host "Report: $ReportJson"
Write-Host "Markdown: $ReportMarkdown"

if ($failed.Count -gt 0) {
    exit 1
}
exit 0
