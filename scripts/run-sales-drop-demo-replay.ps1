param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$EvidenceDir = "",
    [int]$Port = 18066,
    [string]$Namespace = "salesdrop",
    [string]$DataSourceName = "sales-drop-sqlite",
    [string]$BundleName = "sales-drop-models",
    [string]$LauncherJar = "",
    [int]$ReadyTimeoutSeconds = 90,
    [switch]$ReuseExisting,
    [switch]$NoStop
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

function Add-Status {
    param(
        [int]$Step,
        [string]$Name,
        [int]$ExitCode,
        [string]$Result,
        [string]$Output,
        [string]$Error,
        [string]$ValidationMessage = "",
        [bool]$IncludeInSummary = $true
    )

    $script:Results += [pscustomobject]@{
        step = $Step
        name = $Name
        exitCode = $ExitCode
        result = $Result
        output = $Output
        error = $Error
        validationMessage = $ValidationMessage
        includeInSummary = $IncludeInSummary
    }
}

function Invoke-LoggedCommand {
    param(
        [string]$Name,
        [string]$File,
        [string[]]$CommandArgs,
        [scriptblock]$Validator = $null,
        [bool]$IncludeInSummary = $true
    )

    $script:Step += 1
    $safeName = ($Name -replace '[^A-Za-z0-9._-]+', '-').Trim("-")
    $outFile = Join-Path $EvidenceDir ("{0:D2}-{1}.out.log" -f $script:Step, $safeName)
    $errFile = Join-Path $EvidenceDir ("{0:D2}-{1}.err.log" -f $script:Step, $safeName)
    $commandLine = Format-CommandLine -File $File -CommandArgs $CommandArgs

    Add-Content -Path $RunLogPath -Encoding utf8 -Value @(
        "## $($script:Step). $Name",
        "",
        '```powershell',
        $commandLine,
        '```',
        "",
        "Output: $outFile",
        "Error: $errFile",
        ""
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $File @CommandArgs 1> $outFile 2> $errFile
        $exitCode = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
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
    $result = if ($exitCode -eq 0 -and -not $validationMessage) { "passed" } else { "failed" }
    Add-Status -Step $script:Step -Name $Name -ExitCode $exitCode -Result $result -Output $outFile -Error $errFile -ValidationMessage $validationMessage -IncludeInSummary $IncludeInSummary

    Add-Content -Path $RunLogPath -Encoding utf8 -Value @(
        "ExitCode: $exitCode",
        "Result: $result",
        $(if ($validationMessage) { "ValidationMessage: $validationMessage" } else { "ValidationMessage: " }),
        ""
    )

    return [pscustomobject]@{
        name = $Name
        exitCode = $exitCode
        result = $result
        output = $outFile
        error = $errFile
        validationMessage = $validationMessage
        includeInSummary = $IncludeInSummary
    }
}

function Read-JsonFile {
    param([string]$Path)
    return Get-Content -Path $Path -Raw -Encoding utf8 | ConvertFrom-Json
}

function Test-JsonSuccess {
    param([string]$OutputPath)
    try {
        $body = Read-JsonFile -Path $OutputPath
    }
    catch {
        return "Output is not valid JSON: $($_.Exception.Message)"
    }
    if ($body.success -ne $true) {
        return "Runtime envelope success is not true."
    }
    return ""
}

function Test-CapabilitiesJson {
    param([string]$OutputPath)
    $message = Test-JsonSuccess -OutputPath $OutputPath
    if ($message) {
        return $message
    }
    $body = Read-JsonFile -Path $OutputPath
    if ($body.engine -ne "java") {
        return "capabilities engine is $($body.engine), expected java."
    }
    if ($body.runtimeApiVersion -ne "foggy-runtime-api/v1") {
        return "runtimeApiVersion is $($body.runtimeApiVersion)."
    }
    if ($body.data.securityMode -ne "none-dev-test-only") {
        return "securityMode is $($body.data.securityMode), expected none-dev-test-only."
    }
    foreach ($capability in @(
        "runtime.capabilities",
        "datasources.add",
        "datasources.test",
        "datasources.bind",
        "tables.list",
        "tables.inspect",
        "sql.query",
        "models.validate",
        "bundles.list",
        "bundles.add",
        "models.refresh",
        "models.describe",
        "query.validate",
        "query.execute"
    )) {
        $value = $body.data.capabilities.PSObject.Properties[$capability].Value
        if ($value -ne "supported") {
            return "capability $capability is $value."
        }
    }
    return ""
}

function Test-ModelsValidateJson {
    param([string]$OutputPath)
    $message = Test-JsonSuccess -OutputPath $OutputPath
    if ($message) {
        return $message
    }
    $body = Read-JsonFile -Path $OutputPath
    if ($body.data.valid -ne $true) {
        return "models.validate data.valid is not true."
    }
    if ([int]$body.data.invalidFiles -ne 0) {
        return "models.validate invalidFiles is $($body.data.invalidFiles)."
    }
    return ""
}

function Test-ModelsRefreshJson {
    param([string]$OutputPath)
    $message = Test-JsonSuccess -OutputPath $OutputPath
    if ($message) {
        return $message
    }
    $body = Read-JsonFile -Path $OutputPath
    if ([int]$body.data.failedCount -ne 0) {
        return "models.refresh failedCount is $($body.data.failedCount)."
    }
    if ($body.data.refreshedModels -notcontains "SalesDropDailyQueryModel") {
        return "models.refresh did not refresh SalesDropDailyQueryModel."
    }
    return ""
}

function Invoke-Cli {
    param(
        [string]$Name,
        [string[]]$CliArgs,
        [scriptblock]$Validator = ${function:Test-JsonSuccess},
        [bool]$IncludeInSummary = $true
    )

    $baseUrl = "http://127.0.0.1:$Port"
    $pythonArgs = @(
        "-m",
        "foggy_runtime_cli.main",
        "--base-url",
        $baseUrl,
        "--namespace",
        $Namespace
    ) + $CliArgs
    return Invoke-LoggedCommand -Name $Name -File "python" -CommandArgs $pythonArgs -Validator $Validator -IncludeInSummary $IncludeInSummary
}

function Wait-RuntimeReady {
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    $script:WaitReady.status = "waiting"
    $script:WaitReady.startedAt = (Get-Date).ToString("o")
    while ((Get-Date) -lt $deadline) {
        if ($script:StartedProcess -and $script:StartedProcess.HasExited) {
            $script:WaitReady.status = "failed"
            $script:WaitReady.finishedAt = (Get-Date).ToString("o")
            throw "Java runtime exited before readiness. See $ServerOutPath and $ServerErrPath"
        }
        $probe = Invoke-Cli -Name "ready-capabilities" -CliArgs @("capabilities") -Validator ${function:Test-CapabilitiesJson} -IncludeInSummary $false
        $script:WaitReady.attempts += [pscustomobject]@{
            step = $probe.output -replace '^.*\\(\d+)-.*$', '$1'
            result = $probe.result
            exitCode = $probe.exitCode
            output = $probe.output
            error = $probe.error
            validationMessage = $probe.validationMessage
        }
        if ($probe.result -eq "passed") {
            $script:WaitReady.status = "passed"
            $script:WaitReady.finishedAt = (Get-Date).ToString("o")
            return
        }
        Start-Sleep -Seconds 2
    }
    $script:WaitReady.status = "failed"
    $script:WaitReady.finishedAt = (Get-Date).ToString("o")
    throw "Runtime API did not become ready within $ReadyTimeoutSeconds seconds. See $ServerOutPath and $ServerErrPath"
}

function Copy-QuestionPayload {
    param(
        [object]$Case,
        [string]$PayloadPath
    )

    $relativePayload = [string]$Case.payloadFile
    if (-not $relativePayload) {
        throw "Question $($Case.id) does not define payloadFile."
    }
    $sourcePayload = Join-Path $DemoDir $relativePayload
    if (-not (Test-Path $sourcePayload)) {
        throw "Question $($Case.id) payloadFile not found: $sourcePayload"
    }
    Copy-Item -Path $sourcePayload -Destination $PayloadPath -Force
    return $sourcePayload
}

function Test-QuestionAssertions {
    param(
        [string]$OutputPath,
        [object]$Assertions
    )

    if ($null -eq $Assertions) {
        return ""
    }
    try {
        $body = Read-JsonFile -Path $OutputPath
    }
    catch {
        return "Question execute output is not valid JSON: $($_.Exception.Message)"
    }
    if ($body.success -ne $true) {
        return "Question execute envelope success is not true."
    }

    $items = @($body.data.items)
    if ($null -ne $Assertions.rowCountMin -and $items.Count -lt [int]$Assertions.rowCountMin) {
        return "Expected at least $($Assertions.rowCountMin) row(s), got $($items.Count)."
    }

    $requiredColumnAssertions = @()
    if ($Assertions.PSObject.Properties["requiredColumns"]) {
        $requiredColumnAssertions = @($Assertions.requiredColumns)
    }
    $schemaColumns = @($body.data.schema.columns | ForEach-Object { $_.name })
    foreach ($column in $requiredColumnAssertions) {
        if ($schemaColumns -notcontains $column) {
            return "Expected result schema column '$column' was not returned."
        }
    }

    $expectedValueAssertions = @()
    if ($Assertions.PSObject.Properties["expectedValues"]) {
        $expectedValueAssertions = @($Assertions.expectedValues)
    }
    foreach ($expected in $expectedValueAssertions) {
        $field = [string]$expected.field
        $value = [string]$expected.value
        $mismatched = @($items | Where-Object {
            $actual = $_.PSObject.Properties[$field].Value
            [string]$actual -ne $value
        })
        if ($items.Count -gt 0 -and $mismatched.Count -gt 0) {
            return "Expected all result rows to have $field=$value."
        }
    }

    return ""
}

function Invoke-QuestionBankReplay {
    $bank = Read-JsonFile -Path $QuestionBankPath
    $payloadDir = Join-Path $EvidenceDir "question-payloads"
    $caseEvidenceDir = Join-Path $EvidenceDir "question-bank"
    New-Item -ItemType Directory -Force -Path $payloadDir, $caseEvidenceDir | Out-Null

    $qmText = Get-Content -Path $QueryModelPath -Raw -Encoding utf8
    $availableFields = [regex]::Matches($qmText, 'salesDrop\.([A-Za-z][A-Za-z0-9_]*)') |
        ForEach-Object { $_.Groups[1].Value } |
        Sort-Object -Unique
    $availableFieldSet = @{}
    foreach ($field in $availableFields) {
        $availableFieldSet[$field] = $true
    }

    $caseResults = @()
    foreach ($case in $bank.cases) {
        $caseId = [string]$case.id
        $caseStatus = if ($case.status) { [string]$case.status } else { "executable" }
        $requiredFields = @($case.requiredFields)
        $missingFields = @($requiredFields | Where-Object { -not $availableFieldSet.ContainsKey($_) })
        $payloadPath = Join-Path $payloadDir "$caseId.json"

        $result = [ordered]@{
            id = $caseId
            question = $case.question
            declaredStatus = $caseStatus
            expectedBehavior = $case.expectedBehavior
            requiredFields = $requiredFields
            missingFields = $missingFields
            payloadFile = $case.payloadFile
            payload = $null
            sourcePayload = $null
            validate = $null
            execute = $null
            assertionMessage = ""
            skipReason = $case.skipReason
            status = "pending"
            tuningNotes = @()
        }

        if ($missingFields.Count -gt 0) {
            $result.status = "fail"
            $result.tuningNotes += "Missing required fields in SalesDropDailyQueryModel."
            $caseResults += [pscustomobject]$result
            continue
        }

        if ($caseStatus -ne "executable") {
            $result.status = $caseStatus
            if ($case.skipReason) {
                $result.tuningNotes += [string]$case.skipReason
            }
            $caseResults += [pscustomobject]$result
            continue
        }

        try {
            $sourcePayload = Copy-QuestionPayload -Case $case -PayloadPath $payloadPath
            $result.payload = $payloadPath
            $result.sourcePayload = $sourcePayload
        }
        catch {
            $result.status = "fail"
            $result.tuningNotes += $_.Exception.Message
            $caseResults += [pscustomobject]$result
            continue
        }

        $validate = Invoke-Cli -Name "question-$caseId-validate" -CliArgs @("query", "validate", "SalesDropDailyQueryModel", "--payload", $payloadPath)
        $execute = $null
        if ($validate.result -eq "passed") {
            $execute = Invoke-Cli -Name "question-$caseId-execute" -CliArgs @("query", "execute", "SalesDropDailyQueryModel", "--payload", $payloadPath)
        }

        $result.validate = $validate.output
        if ($execute) {
            $result.execute = $execute.output
        }
        if ($validate.result -eq "passed" -and $execute -and $execute.result -eq "passed") {
            $assertionMessage = Test-QuestionAssertions -OutputPath $execute.output -Assertions $case.assertions
            $result.assertionMessage = $assertionMessage
        }
        if ($validate.result -eq "passed" -and $execute -and $execute.result -eq "passed" -and -not $result.assertionMessage) {
            $result.status = "pass"
        }
        else {
            $result.status = "fail"
            if ($result.assertionMessage) {
                $result.tuningNotes += $result.assertionMessage
            }
            else {
                $result.tuningNotes += "Validate or execute failed; inspect linked evidence."
            }
        }
        $caseResults += [pscustomobject]$result
    }

    $passCount = @($caseResults | Where-Object { $_.status -eq "pass" }).Count
    $failCount = @($caseResults | Where-Object { $_.status -eq "fail" }).Count
    $clarifyCount = @($caseResults | Where-Object { $_.status -eq "needs-clarification" }).Count
    $unsupportedCount = @($caseResults | Where-Object { $_.status -eq "unsupported" }).Count
    $coverage = [ordered]@{
        schemaVersion = "foggy-demo-question-bank-replay/v1"
        generatedAt = (Get-Date).ToString("o")
        questionBank = $QuestionBankPath
        questionBankSchemaVersion = $bank.schemaVersion
        model = $bank.model
        availableFields = $availableFields
        totalCases = @($caseResults).Count
        passCases = $passCount
        failCases = $failCount
        needsClarificationCases = $clarifyCount
        unsupportedCases = $unsupportedCount
        cases = $caseResults
    }
    $coveragePath = Join-Path $EvidenceDir "question-bank-replay.json"
    $coverage | ConvertTo-Json -Depth 12 | Out-File -FilePath $coveragePath -Encoding utf8

    if ($failCount -gt 0) {
        throw "Question bank replay has $failCount failed case(s). See $coveragePath"
    }
    return $coveragePath
}

if (-not $EvidenceDir) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $EvidenceDir = Resolve-DefaultPath -Base $RepoRoot -Relative ".codex-tmp\foggy-ai-analysis-demo\sales-drop-replay-$stamp"
}
if (-not $LauncherJar) {
    $LauncherJar = Resolve-DefaultPath -Base $RepoRoot -Relative "foggy-data-mcp-bridge-wt-dev-compose\foggy-mcp-launcher\target\foggy-mcp-launcher-9.1.0.beta.jar"
}

$DemoDir = Resolve-DefaultPath -Base $RepoRoot -Relative ".codex\skills\foggy-ai-analysis-demo\assets\sales-drop-demo"
$SchemaPath = Join-Path $DemoDir "schema.sql"
$DataPath = Join-Path $DemoDir "data.sql"
$ModelsDir = Join-Path $DemoDir "models"
$QueryPayload = Join-Path $DemoDir "queries\basic.json"
$QuestionBankPath = Join-Path $DemoDir "question-bank.json"
$QueryModelPath = Join-Path $ModelsDir "query\SalesDropDailyQueryModel.qm"
$CliSrc = Resolve-DefaultPath -Base $RepoRoot -Relative "foggy-runtime-cli\src"
$SqlitePath = Join-Path $EvidenceDir "sales_drop_demo.sqlite"
$BundleRegistryPath = Join-Path $EvidenceDir "runtime-bundles.json"
$DatasourceRegistryPath = Join-Path $EvidenceDir "runtime-datasources.json"
$ServerOutPath = Join-Path $EvidenceDir "java-runtime.stdout.log"
$ServerErrPath = Join-Path $EvidenceDir "java-runtime.stderr.log"
$RunLogPath = Join-Path $EvidenceDir "run-log.md"
$SummaryPath = Join-Path $EvidenceDir "summary.json"
$MarkdownSummaryPath = Join-Path $EvidenceDir "evidence-summary.md"
$CommandStatusPath = Join-Path $EvidenceDir "command-status.csv"
$script:Step = 0
$script:Results = @()
$script:StartedProcess = $null
$script:WaitReady = [ordered]@{
    timeoutSeconds = $ReadyTimeoutSeconds
    retryIntervalSeconds = 2
    status = "not-started"
    startedAt = $null
    finishedAt = $null
    attempts = @()
}
$oldPythonPath = $env:PYTHONPATH

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

Set-Content -Path $RunLogPath -Encoding utf8 -Value @(
    "# Sales Drop Demo Replay",
    "",
    "- EvidenceDir: $EvidenceDir",
    "- RuntimeUrl: http://127.0.0.1:$Port",
    "- Port: $Port",
    "- Namespace: $Namespace",
    "- SQLite: $SqlitePath",
    "- LauncherJar: $LauncherJar",
    "- StartedAt: $(Get-Date -Format o)",
    ""
)

try {
    foreach ($requiredPath in @($CliSrc, $LauncherJar, $SchemaPath, $DataPath, $ModelsDir, $QueryPayload, $QuestionBankPath, $QueryModelPath)) {
        if (-not (Test-Path $requiredPath)) {
            throw "Required path not found: $requiredPath"
        }
    }

    $env:PYTHONPATH = if ($oldPythonPath) { "$CliSrc;$oldPythonPath" } else { $CliSrc }

    Invoke-LoggedCommand -Name "java-version" -File "java" -CommandArgs @("-version") | Out-Null
    Invoke-LoggedCommand -Name "python-version" -File "python" -CommandArgs @("--version") | Out-Null

    $seedCode = @"
import pathlib, sqlite3
db = pathlib.Path(r'$SqlitePath')
schema = pathlib.Path(r'$SchemaPath').read_text(encoding='utf-8')
data = pathlib.Path(r'$DataPath').read_text(encoding='utf-8')
db.parent.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(db)
con.executescript(schema)
con.executescript(data)
con.commit()
con.close()
print(db)
"@
    Invoke-LoggedCommand -Name "seed-sales-drop-sqlite" -File "python" -CommandArgs @("-c", $seedCode) | Out-Null

    if (Test-PortOpen -TargetPort $Port) {
        if (-not $ReuseExisting) {
            throw "Port $Port is already open. Use -ReuseExisting or choose another -Port."
        }
        Add-Content -Path $RunLogPath -Encoding utf8 -Value @("## Runtime", "", "Reusing existing runtime on port $Port.", "")
    }
    else {
        $javaArgs = @(
            "-Dfile.encoding=UTF-8",
            "-jar", $LauncherJar,
            "--server.port=$Port",
            "--spring.profiles.active=lite",
            "--foggy.runtime-api.enabled=true",
            "--foggy.runtime-api.bundle-registry.path=$BundleRegistryPath",
            "--foggy.runtime-api.datasource-registry.path=$DatasourceRegistryPath",
            "--foggy.data-viewer.enabled=false",
            "--foggy.mcp.audit.enabled=false",
            "--spring.datasource.url=jdbc:sqlite:$SqlitePath",
            "--spring.ai.openai.api-key=sk-runtime-demo",
            "--spring.ai.openai.base-url=http://127.0.0.1:9",
            "--spring.ai.openai.chat.options.model=runtime-demo"
        )
        Add-Content -Path $RunLogPath -Encoding utf8 -Value @(
            "## Start Java lite runtime",
            "",
            '```powershell',
            (Format-CommandLine -File "java" -CommandArgs $javaArgs),
            '```',
            "",
            "Stdout: $ServerOutPath",
            "Stderr: $ServerErrPath",
            ""
        )
        $script:StartedProcess = Start-Process -FilePath "java" `
            -ArgumentList $javaArgs `
            -RedirectStandardOutput $ServerOutPath `
            -RedirectStandardError $ServerErrPath `
            -PassThru `
            -WindowStyle Hidden
        $script:StartedProcess.Id | Out-File -FilePath (Join-Path $EvidenceDir "server.pid") -Encoding utf8
        Add-Content -Path $RunLogPath -Encoding utf8 -Value @("PID: $($script:StartedProcess.Id)", "")
    }

    Wait-RuntimeReady

    Invoke-Cli -Name "capabilities" -CliArgs @("capabilities") -Validator ${function:Test-CapabilitiesJson} | Out-Null
    Invoke-Cli -Name "datasources-add-sales-drop-sqlite" -CliArgs @("datasources", "add", "--name", $DataSourceName, "--type", "sqlite", "--jdbc-url", "jdbc:sqlite:$SqlitePath", "--replace") | Out-Null
    Invoke-Cli -Name "datasources-test-sales-drop-sqlite" -CliArgs @("datasources", "test", $DataSourceName) | Out-Null
    Invoke-Cli -Name "datasources-bind-salesdrop-sales-drop-sqlite" -CliArgs @("datasources", "bind", "--namespace", $Namespace, "--data-source", $DataSourceName) | Out-Null
    Invoke-Cli -Name "tables-list-sales-drop-sqlite" -CliArgs @("tables", "list", "--data-source", $DataSourceName) | Out-Null
    Invoke-Cli -Name "tables-inspect-sales_drop_daily" -CliArgs @("tables", "inspect", "--data-source", $DataSourceName, "--table", "sales_drop_daily", "--include-indexes") | Out-Null
    Invoke-Cli -Name "sql-query-sales-drop-top5" -CliArgs @("sql", "query", "--data-source", $DataSourceName, "--sql", "select sales_drop_id, observation_date, region, channel, severity, root_cause, sales_drop_amount, sales_drop_rate from sales_drop_daily order by sales_drop_amount desc", "--max-rows", "5", "--timeout-seconds", "5") | Out-Null
    Invoke-Cli -Name "models-validate" -CliArgs @("models", "validate", "--models-dir", $ModelsDir) -Validator ${function:Test-ModelsValidateJson} | Out-Null
    Invoke-Cli -Name "bundles-list-before-add" -CliArgs @("bundles", "list") | Out-Null
    Invoke-Cli -Name "bundles-add-sales-drop-models" -CliArgs @("bundles", "add", "--name", $BundleName, "--path", $ModelsDir, "--watch", "--replace") | Out-Null
    Invoke-Cli -Name "models-refresh" -CliArgs @("models", "refresh") -Validator ${function:Test-ModelsRefreshJson} | Out-Null
    Invoke-Cli -Name "models-describe-SalesDropDailyQueryModel" -CliArgs @("models", "describe", "SalesDropDailyQueryModel") | Out-Null
    Invoke-Cli -Name "query-validate-basic" -CliArgs @("query", "validate", "SalesDropDailyQueryModel", "--payload", $QueryPayload) | Out-Null
    Invoke-Cli -Name "query-execute-basic" -CliArgs @("query", "execute", "SalesDropDailyQueryModel", "--payload", $QueryPayload) | Out-Null

    $questionBankEvidence = Invoke-QuestionBankReplay

    $failed = @($script:Results | Where-Object { $_.includeInSummary -and $_.result -ne "passed" })
    $status = if ($failed.Count -eq 0) { "passed" } else { "failed" }
    $summary = [ordered]@{
        schemaVersion = "foggy-demo-evidence/v1"
        generatedAt = (Get-Date).ToString("o")
        status = $status
        runtimeUrl = "http://127.0.0.1:$Port"
        port = $Port
        namespace = $Namespace
        dataSource = $DataSourceName
        bundle = $BundleName
        sqlitePath = $SqlitePath
        evidenceDir = $EvidenceDir
        launcherJar = $LauncherJar
        modelsDir = $ModelsDir
        queryPayload = $QueryPayload
        questionBank = $QuestionBankPath
        questionBankEvidence = $questionBankEvidence
        commandStatus = $CommandStatusPath
        waitReady = $script:WaitReady
        results = $script:Results
        notes = @(
            "This script is for trusted local technical/developer use with full runtime privileges.",
            "Production permission/auth/governance design is intentionally deferred."
        )
    }
    $script:Results | Export-Csv -Path $CommandStatusPath -NoTypeInformation -Encoding utf8
    $summary | ConvertTo-Json -Depth 12 | Out-File -FilePath $SummaryPath -Encoding utf8

    $summaryMarkdown = @(
        "# Sales Drop Demo Replay Summary",
        "",
        "- Status: $status",
        "- Runtime URL: http://127.0.0.1:$Port",
        "- Namespace: $Namespace",
        "- SQLite: $SqlitePath",
        "- Evidence: $EvidenceDir",
        "- Command log: $RunLogPath",
        "- Command status: $CommandStatusPath",
        "- Question bank evidence: $questionBankEvidence",
        "- Runtime stopped by script: $([bool]($script:StartedProcess -and -not $NoStop))",
        "",
        "## Scope",
        "",
        "This replay is intended for trusted local technical/developer use. It assumes the operator has maximum Runtime API privileges. Production permission, auth, and governance design is deferred.",
        "",
        "## Results",
        ""
    )
    foreach ($item in $script:Results) {
        $line = "- $($item.name): $($item.result) (exit $($item.exitCode))"
        if (-not $item.includeInSummary) {
            $line += " - ignored in final status"
        }
        if ($item.validationMessage) {
            $line += " - $($item.validationMessage)"
        }
        $summaryMarkdown += $line
    }
    $summaryMarkdown | Out-File -FilePath $MarkdownSummaryPath -Encoding utf8

    if ($failed.Count -gt 0) {
        Write-Error "Sales-drop replay failed. See $SummaryPath"
        exit 1
    }

    Write-Host "Sales-drop replay passed. Evidence: $EvidenceDir"
    Write-Host "Summary: $SummaryPath"
    Write-Host "Question bank: $questionBankEvidence"
    exit 0
}
catch {
    $failureMessage = $_.Exception.Message
    $script:Results | Export-Csv -Path $CommandStatusPath -NoTypeInformation -Encoding utf8
    $summary = [ordered]@{
        schemaVersion = "foggy-demo-evidence/v1"
        generatedAt = (Get-Date).ToString("o")
        status = "failed"
        failure = $failureMessage
        runtimeUrl = "http://127.0.0.1:$Port"
        port = $Port
        namespace = $Namespace
        dataSource = $DataSourceName
        bundle = $BundleName
        sqlitePath = $SqlitePath
        evidenceDir = $EvidenceDir
        launcherJar = $LauncherJar
        modelsDir = $ModelsDir
        queryPayload = $QueryPayload
        questionBank = $QuestionBankPath
        questionBankEvidence = $null
        commandStatus = $CommandStatusPath
        waitReady = $script:WaitReady
        runtimeStdout = $ServerOutPath
        runtimeStderr = $ServerErrPath
        results = $script:Results
        notes = @(
            "This script is for trusted local technical/developer use with full runtime privileges.",
            "Production permission/auth/governance design is intentionally deferred."
        )
    }
    $summary | ConvertTo-Json -Depth 12 | Out-File -FilePath $SummaryPath -Encoding utf8
    @(
        "# Sales Drop Demo Replay Summary",
        "",
        "- Status: failed",
        "- Failure: $failureMessage",
        "- Runtime URL: http://127.0.0.1:$Port",
        "- Namespace: $Namespace",
        "- SQLite: $SqlitePath",
        "- Evidence: $EvidenceDir",
        "- Command log: $RunLogPath",
        "- Command status: $CommandStatusPath",
        "- Runtime stdout: $ServerOutPath",
        "- Runtime stderr: $ServerErrPath",
        "",
        "## Scope",
        "",
        "This replay is intended for trusted local technical/developer use. It assumes the operator has maximum Runtime API privileges. Production permission, auth, and governance design is deferred.",
        "",
        "## Results",
        ""
    ) | Out-File -FilePath $MarkdownSummaryPath -Encoding utf8
    foreach ($item in $script:Results) {
        $line = "- $($item.name): $($item.result) (exit $($item.exitCode))"
        if (-not $item.includeInSummary) {
            $line += " - ignored in final status"
        }
        if ($item.validationMessage) {
            $line += " - $($item.validationMessage)"
        }
        Add-Content -Path $MarkdownSummaryPath -Encoding utf8 -Value $line
    }
    Write-Error "Sales-drop replay failed. See $SummaryPath. $failureMessage"
    exit 1
}
finally {
    $env:PYTHONPATH = $oldPythonPath
    if ($script:StartedProcess -and -not $NoStop) {
        if (-not $script:StartedProcess.HasExited) {
            Stop-Process -Id $script:StartedProcess.Id -Force
            Add-Content -Path $RunLogPath -Encoding utf8 -Value @("## Stop runtime", "", "Stopped Java runtime pid=$($script:StartedProcess.Id)", "")
        }
    }
}
