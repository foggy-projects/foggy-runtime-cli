# foggy-runtime-cli

Standalone CLI transport for `Foggy Runtime API v1` and the independent
`Analytics Runtime API v1`.

The CLI talks to public `/api/v1/*` operations under the configured base URL.
`query open-in-viewer` uses the public MCP endpoint on the same server. It does
not call Java or Python engine private routes.

## Opaque Onboarding Profiles

`profiles` separates trusted datasource setup from an agent-visible onboarding
session. A trusted operator creates the private profile once; DeepSeek Harness or
another agent receives only a random profile ID, an immutable SHA-256 revision,
the datasource name/type, namespace, and approval requirements.

Private input accepts `name`, `type`, `jdbcUrl`, optional `username`, optional
`passwordEnv`, and `namespace`. Password values are never accepted, JDBC URLs
containing passwords are rejected, and there is deliberately no command that
prints or resolves private connection material.

```powershell
# Run this trusted setup step outside the Harness session.
foggy-runtime profiles create --input .\connection.private.json

# Review the opaque summary. Neither command prints JDBC/username/passwordEnv.
foggy-runtime profiles list
foggy-runtime profiles show fop_<32-hex>

# Harness can dry-run with only the ID and the reviewed revision.
foggy-runtime --base-url http://127.0.0.1:18066 profiles apply fop_<32-hex> `
  --approve-revision sha256:<64-hex>

# Configuration and namespace binding are separately explicit mutations.
foggy-runtime --base-url http://127.0.0.1:18066 profiles apply fop_<32-hex> `
  --approve-revision sha256:<64-hex> --approve-configure --approve-bind
```

Profiles default to `%LOCALAPPDATA%\Foggy\runtime-cli\profiles` on Windows and
`$XDG_CONFIG_HOME/foggy-runtime/profiles` (or `~/.config/foggy-runtime/profiles`)
elsewhere. `FOGGY_RUNTIME_PROFILE_STORE` overrides this location for controlled
deployments and tests. The CLI uses atomic writes, owner-only POSIX modes where
the platform supports them, rejects symlink profile paths, and requires the exact
stored revision for apply, replacement, and removal.

Profiles are a CLI configuration and output boundary for Harness integration;
operating-system process isolation is outside this component's scope.

## Analytics Runtime Development Lane

The development line installs both `foggy-runtime` and `foggy` executable
aliases. Existing flat Runtime commands remain compatible, while the named
domains are:

```bash
foggy runtime capabilities
foggy analytics capabilities
foggy analytics bundles list
foggy analytics bundles validate sales --revision sha256:<64-hex>
foggy analytics reports preview sales-summary \
  --bundle sales \
  --revision sha256:<64-hex> \
  --authority-provider tms \
  --authority-reference subject:42
foggy analytics dashboards render sales-board \
  --bundle sales \
  --revision sha256:<64-hex> \
  --authority-provider console \
  --authority-reference session:7
```

Analytics uses `FOGGY_ANALYTICS_RUNTIME_API_URL`; its local co-hosted default is
`http://127.0.0.1:8080/analytics`. Optional credentials use
`FOGGY_ANALYTICS_RUNTIME_API_AUTH_CODE` and `FOGGY_ANALYTICS_AUTHORIZATION`.
They do not fall back to the corresponding Foggy Runtime variables. Analytics
Bundle roots, owner/ACL metadata, raw SQL, and raw permission filters are not
accepted by these commands.

## Embedded Analytics Console

New standard Foggy Runtime Launcher releases can embed the Analytics Console backend and
prebuilt SPA in the executable JAR. Users do not install Node.js or a separate Console
package. Check the downloaded `runtime-launcher-manifest.json` before enabling it:

```powershell
$manifest = Get-Content .\runtime-launcher-manifest.json -Raw | ConvertFrom-Json
$manifest.features.analyticsConsole
```

Only use the Console switch when `embedded=true`. It remains disabled by default:

```powershell
.\start-foggy-runtime.ps1 -AnalyticsConsole
```

```bash
ANALYTICS_CONSOLE_ENABLED=true ./start-foggy-runtime.sh
```

Then verify Runtime API readiness and open `/analytics-console/`. FAP is an optional,
separately managed integration and remains disabled; the launcher never creates FAP
providers, Skills, Capabilities, Functions, credentials, or workspace bindings. The
`stack show` resolves the published stable manifest; `stack show --offline-stack`
uses the verified release versions bundled with this CLI. Check the selected
launcher's manifest for embedded Console and Analytics Runtime API support and
keep both disabled unless explicitly needed. FAP remains disabled and host-managed.

## Public AI Analysis Demo Quick Start

Public release inputs (the online stable manifest may move independently):

- CLI: `v0.1.24`
- `foggy-ai-analysis` Skill: `v0.1.18`
- optional `foggy-semantic-query` Skill: `v0.1.18`
- Foggy Runtime Launcher: `foggy-runtime-launcher-v0.1.24`
- Stable stack manifest: `https://raw.githubusercontent.com/foggy-projects/foggy-ai-analysis/main/stack/stable.json`
- Runtime URL: `http://127.0.0.1:18066`
- Namespace: `salesdrop`
- Datasource mode: Java runtime default SQLite datasource

Prerequisites: Python with pip, Java on `PATH`, and PowerShell.

Copy this PowerShell path to install the CLI, download the Skill and Foggy Runtime Launcher, start the local runtime, and replay the sales-drop demo:

```powershell
$stackManifestUrl = "https://raw.githubusercontent.com/foggy-projects/foggy-ai-analysis/main/stack/stable.json"
$stack = Invoke-RestMethod -Uri $stackManifestUrl
$cliVersion = $stack.components.cli.recommendedVersion
$skillVersion = $stack.components.skills.'foggy-ai-analysis'.recommendedVersion
$launcher = $stack.components.launcher
$demoRoot = Join-Path $env:TEMP "foggy-ai-analysis-$skillVersion"
$installDir = Join-Path $demoRoot "cli-install"
$launcherDir = Join-Path $demoRoot "launcher"
$runtimeDir = Join-Path $demoRoot "runtime"
$evidenceDir = Join-Path $demoRoot "evidence"

foreach ($dir in @($installDir, $launcherDir, $runtimeDir, $evidenceDir)) {
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

# 1. Install foggy-runtime-cli from the public release.
Invoke-WebRequest `
  -Uri $stack.components.cli.install.windows `
  -OutFile (Join-Path $installDir "install-foggy-runtime-cli.ps1")
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $installDir "install-foggy-runtime-cli.ps1") -Version $cliVersion

# 2. Install the formal foggy-ai-analysis Skill from the stable stack recommendation.
foggy-runtime stack show
foggy-runtime skills install foggy-ai-analysis --replace
$skillDir = Join-Path $env:USERPROFILE ".agents\skills\foggy-ai-analysis"

# 3. Download the Foggy Runtime Launcher from the same stable stack recommendation.
foreach ($asset in @(
  $launcher.assets.jar,
  $launcher.assets.startPowerShell,
  $launcher.assets.startShell,
  $launcher.assets.manifest,
  $launcher.assets.checksums
)) {
  Invoke-WebRequest -Uri $asset.url -OutFile (Join-Path $launcherDir $asset.file)
}

# 4. Start Java with a known SQLite default datasource.
Push-Location $launcherDir
$start = .\start-foggy-runtime.ps1 -Port 18066 -WorkDir $runtimeDir | ConvertFrom-Json
Pop-Location
$sqlite = $start.sqlitePath

# 5. Verify readiness and replay the sales-drop question bank.
foggy-runtime --base-url $start.runtimeUrl --namespace salesdrop wait-ready --timeout-seconds 90 --interval-seconds 2
foggy-runtime --base-url $start.runtimeUrl demo sales-drop replay `
  --skill-dir $skillDir `
  --evidence-dir $evidenceDir `
  --sqlite-path $sqlite `
  --use-default-datasource

# 6. Stop the local dev/test runtime when finished.
Stop-Process -Id $start.pid -ErrorAction SilentlyContinue
```

Expected replay result:

```text
question-bank total=12 executable=11 pass=11 fail=0 needs-clarification=1
```

Current boundary: `--use-default-datasource` is still the public sales-drop replay default because that example owns and reseeds a local SQLite file. For user business data, keep the user datasource separate from sales-drop demo data: register a Runtime API-managed datasource, bind it to the target namespace, validate the bundle, refresh, describe, and run a query smoke against that namespace. Use `datasources diagnostics` to record registry paths and namespace bindings before restart evidence. The launcher is dev/test-only and reports `securityMode=none-dev-test-only`; production permission, auth, RBAC, audit, and governance are deferred.

For `foggy-ai-analysis` Skill v0.1.18, the replay must reseed the Java runtime default SQLite file with the Skill-bundled `schema.sql` and `data.sql`. Reusing an older `sales_drop_daily` table can leave out customer dimension columns required by the current TM/QM.

For Runtime API management operations against runtimes configured with `securityMode=auth-code`, use CLI `v0.1.6` or later and pass `--auth-code` or `FOGGY_RUNTIME_API_AUTH_CODE`. This auth-code path is separate from the current public sales-drop demo baseline above.

Copyable first prompt for an LLM session:

```text
Use foggy-runtime-cli v0.1.24, the formal foggy-ai-analysis Skill v0.1.18 release assets, and Foggy Runtime Launcher `foggy-runtime-launcher-v0.1.24`. Install the Skill into ~/.agents/skills/foggy-ai-analysis, start the Java runtime on http://127.0.0.1:18066 with a SQLite default datasource, then run foggy-runtime demo sales-drop replay with --use-default-datasource and the same SQLite path so the bundled schema.sql/data.sql reseeds the runtime default SQLite file. For user business data, use a separate Runtime API-managed datasource and namespace binding instead of mixing it with the sales-drop SQLite file. Record commands, checksums, runtime URL, namespace, datasource mode, diagnostics, question-bank totals, evidence files, failures, and fixes. Production permission/auth/RBAC/audit/governance are out of scope for this demo.
```

## Installation

Windows PowerShell from GitHub Release:

```powershell
$version = "0.1.24"
$download = Join-Path $env:TEMP "foggy-runtime-cli-install-$version"
New-Item -ItemType Directory -Force -Path $download | Out-Null
Invoke-WebRequest `
  -Uri "https://github.com/foggy-projects/foggy-runtime-cli/releases/download/v$version/install-foggy-runtime-cli.ps1" `
  -OutFile (Join-Path $download "install-foggy-runtime-cli.ps1")
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $download "install-foggy-runtime-cli.ps1") -Version $version
foggy-runtime --version
foggy-runtime --help
python -m pip show foggy-runtime-cli
```

Linux/macOS from GitHub Release:

```bash
version="0.1.24"
download="${TMPDIR:-/tmp}/foggy-runtime-cli-install-$version"
mkdir -p "$download"
curl -fsSL "https://github.com/foggy-projects/foggy-runtime-cli/releases/download/v$version/install-foggy-runtime-cli.sh" -o "$download/install-foggy-runtime-cli.sh"
bash "$download/install-foggy-runtime-cli.sh" --version "$version"
foggy-runtime --version
foggy-runtime --help
python -m pip show foggy-runtime-cli
```

From a released wheel:

```powershell
python -m pip install foggy_runtime_cli-0.1.24-py3-none-any.whl
foggy-runtime --version
foggy-runtime --help
```

The release installers download the wheel and `SHA256SUMS`, verify the wheel hash, install with pip, and print a short CLI help excerpt. Use `--python <python-exe>` or `-Python <python-exe>` when the target Python is not the default `python` on `PATH`.

From source:

```powershell
git clone https://github.com/foggy-projects/foggy-runtime-cli.git
cd foggy-runtime-cli
python -m pip install .
foggy-runtime --help
```

## Skill Install Target

`foggy-runtime skills install` installs supported Foggy Skills only under the agent Skill directory:

```text
~/.agents/skills/<skill-name>
```

It does not install or update copies under `~/.codex/skills` or `~/.claude/skills`.

Stable stack install:

```powershell
foggy-runtime stack show
foggy-runtime skills install foggy-ai-analysis --replace
foggy-runtime skills install foggy-semantic-query --replace
```

Pinned release zip install:

```powershell
foggy-runtime skills install foggy-ai-analysis --zip .\foggy-ai-analysis-skill-0.1.18.zip --replace
```

Supported local workspace installs:

```powershell
foggy-runtime skills install foggy-ai-analysis --workspace-root D:\foggy-projects\foggy-data-mcp
foggy-runtime skills install foggy-semantic-query --workspace-root D:\foggy-projects\foggy-data-mcp
foggy-runtime skills install foggy-analysis-suite --workspace-root D:\foggy-projects\foggy-data-mcp
```

`foggy-analysis-suite` installs both `foggy-ai-analysis` and `foggy-semantic-query`; without `--workspace-root`, it downloads both release zips from the stable stack manifest.

For source maintenance, `--workspace-root` also accepts the independent `foggy-ai-analysis` checkout or its parent workspace. The installer prefers `locales/en` and `skills/foggy-semantic-query` in that checkout over legacy `.codex/skills` copies. Edit and commit those canonical Skill sources; the CLI contains installation logic, not another copy of the Skill instructions. Source-only changes do not require rebuilding packages or advancing the stable stack.

## Release Packaging

Build local release artifacts:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1 -Clean
```

Linux/macOS:

```bash
bash scripts/build-release.sh --clean
```

The release build runs tests by default and writes the exact six publishable assets into `dist/`:
wheel, sdist, both installers, `SHA256SUMS`, and `release-manifest.json`. New manifests declare
`checksumCoverage=all-release-assets`, so one `sha256sum -c dist/SHA256SUMS` verifies every asset
except the checksum file itself.

GitHub Release is the authoritative CLI distribution source. After a GitHub release is verified, an
operator may explicitly mirror its exact six assets to the company download server with the
workspace `scripts/publish-foggy-github-release-to-company.sh` command. That optional mirror returns
permanent `download.qlfloor.com` links. OBS is not part of the default release path; its workflow is
manual-only.

GitHub releases are created from tags by `.github/workflows/release.yml`:

```powershell
git tag -a v0.1.24 -m "Release v0.1.24"
git push origin v0.1.24
```

Release assets include:

- `foggy_runtime_cli-<version>-py3-none-any.whl`
- `foggy_runtime_cli-<version>.tar.gz`
- `SHA256SUMS`
- `release-manifest.json`
- `install-foggy-runtime-cli.ps1`
- `install-foggy-runtime-cli.sh`

The formal public Skill release is published from `foggy-projects/foggy-ai-analysis`, not from the CLI release. Download and install the Skill zip when you need the bundled onboarding and sales-drop demo assets:

- `foggy-ai-analysis-skill-<version>.zip`
- `foggy-ai-analysis-skill-<version>-manifest.json`
- `foggy-ai-analysis-skill-<version>-SHA256SUMS`

## Examples

```powershell
foggy-runtime stack show
foggy-runtime --base-url http://127.0.0.1:8080 capabilities
foggy-runtime --base-url http://127.0.0.1:8080 wait-ready --timeout-seconds 90 --interval-seconds 2
foggy-runtime --auth-code $env:FOGGY_RUNTIME_API_AUTH_CODE bundles add --name sales-drop-dev --path ./models --namespace default --watch
foggy-runtime bundles list
foggy-runtime bundles add --name sales-drop-dev --path ./models --namespace default --watch --validate --refresh
foggy-runtime bundles update sales-drop-dev --path ./models --watch
foggy-runtime bundles remove sales-drop-dev
foggy-runtime datasources add --name local-sqlite --type sqlite --jdbc-url jdbc:sqlite:./runtime.db --replace
foggy-runtime datasources add --name sales-mysql --type mysql --jdbc-url "jdbc:mysql://127.0.0.1:13308/foggy_demo?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC" --username foggy --password-env FOGGY_MYSQL_PASSWORD --replace
foggy-runtime datasources binding --namespace default
foggy-runtime datasources diagnostics
foggy-runtime resources pull --bundle sales-drop-dev --out ./work-models
foggy-runtime resources save --bundle sales-drop-dev --dir ./work-models --validate --refresh
foggy-runtime models list
foggy-runtime models describe FactSalesQueryModel
foggy-runtime models refresh --model FactSalesQueryModel
foggy-runtime models validate --models-dir ./models
foggy-runtime query validate FactSalesQueryModel --payload query.json
foggy-runtime query execute FactSalesQueryModel --payload -
foggy-runtime members list FactSalesQueryModel orderStatus
foggy-runtime query explain FactSalesQueryModel --field totalAmount --include-physical-names
foggy-runtime query explain FactSalesQueryModel --payload query.json --include-sql
foggy-runtime compose validate --script compose.fsscript
foggy-runtime compose preview --script compose.fsscript
foggy-runtime compose execute --script compose.fsscript
foggy-runtime fsscript run --script workflow.fsscript
foggy-runtime fsscript run --script workflow.fsscript --enable-cte-bridge
foggy-runtime tables inspect --table sale_order --schema public --include-indexes
foggy-runtime demo sales-drop plan --repo-root D:\foggy-projects\foggy-data-mcp --port 18066
foggy-runtime demo sales-drop plan --repo-root D:\foggy-projects\foggy-data-mcp --skill-dir D:\demo\skills\foggy-ai-analysis --port 18066
foggy-runtime --base-url http://127.0.0.1:18066 demo sales-drop replay --skill-dir D:\demo\skills\foggy-ai-analysis --evidence-dir D:\demo\evidence --sqlite-path D:\demo\runtime\sales_drop_demo.sqlite --use-default-datasource
```

Query payload compatibility:

```json
{
  "columns": ["customerName", "customerSegment", "observationDate$yearMonth", "sum(salesDropAmount) as totalDrop"],
  "groupBy": ["customerName", "customerSegment", "observationDate$yearMonth"]
}
```

The date grain must be returned by `models describe`, typically from a TM self date dimension or a
physical date dimension. The CLI does not append `$year`, `$month`, or `$yearMonth` to a plain
`DATE` / `DATETIME` property.

The CLI accepts `groupBy` string-array shorthand in query payloads and normalizes it to Runtime API v1 object items before sending the request. Raw Runtime API v1 HTTP callers that bypass the CLI should send `groupBy` as `[{"field":"customerName"}]` until the Java API adds native string-array compatibility.

Static dictionary captions are a Runtime capability, not a CLI-side formatting feature. When
`models describe <QueryModel>` exposes both `orderStatus` and `orderStatus$caption`, the CLI sends
them unchanged and preserves the Runtime result: the raw field remains the database code and the
caption field contains the registered label. A registered label can be sent to the Java Runtime in
an equality/set filter on the raw field or caption field; the Runtime converts it to the field's
typed code before SQL binding.

```json
{
  "columns": ["orderStatus", "orderStatus$caption", "orderCount"],
  "slice": [
    {"field": "orderStatus", "op": "=", "value": "Awaiting pickup"}
  ],
  "groupBy": ["orderStatus", "orderStatus$caption"],
  "orderBy": ["-orderCount"],
  "limit": 20
}
```

Validate and execute that payload normally, and enumerate its registered choices through
`foggy-runtime members list <QueryModel> orderStatus`. Do not order by `$caption` or apply
range/fuzzy operators to it; model a database-backed dimension when label ordering is required.
If describe does not advertise `$caption`, do not generate it. The CLI deliberately does not keep
a dictionary mapping or perform a second conversion.

JSON output is the default and preserves the Runtime API envelope for Skill consumption.

The CLI configures stdout and stderr as UTF-8 when it owns the process streams, so Windows PowerShell JSON evidence can preserve non-ASCII model metadata. If a host wrapper overrides Python stream configuration, set `PYTHONUTF8=1` or `PYTHONIOENCODING=utf-8` before running `foggy-runtime`.

The CLI is backend-neutral and does not select Java or Python. `--base-url` always wins, followed by `FOGGY_RUNTIME_API_URL`, then the local development default `http://127.0.0.1:8080`.

When the connected Runtime API reports `securityMode=auth-code`, pass the shared runtime code with global `--auth-code <code>` or `FOGGY_RUNTIME_API_AUTH_CODE`. The CLI sends it as `X-Foggy-Runtime-Code`, which is required for protected management operations such as bundle add/update/remove, datasource add/test/bind, resources save, models validate, and models refresh. Runtimes using `none-dev-test-only` do not require this option.

Data-plane model permissions use a separate optional credential. Pass the complete opaque header value with global `--authorization <value>` or `FOGGY_RUNTIME_AUTHORIZATION`; the CLI sends it unchanged as `Authorization` only to model list/describe, query, Compose, dimension-member paths, and POST requests to the supported analyst/business MCP endpoints. Redirects do not forward this header, and echoed values are redacted from responses and transport errors.

### Open a data view

`query open-in-viewer` discovers `dataset.open_in_viewer` through `tools/list`
and calls it with the query DSL. Use the Authorization header already configured
for the MCP service, supplied securely through `FOGGY_RUNTIME_AUTHORIZATION`,
and its namespace. No separate viewer username or password is required.

```powershell
foggy-runtime --base-url http://127.0.0.1:18066 --namespace demo `
  query open-in-viewer DemoWaybillQueryModel --payload .\by-station.json `
  --title "Freight by station"
```

The default MCP path is `/mcp/analyst/rpc`. Select the configured endpoint with
`--mcp-path` when needed; `/mcp/analyst`, `/mcp/business/rpc`, and `/mcp/business`
are also supported. The base URL can include a server context path. An endpoint
that does not advertise the viewer tool fails with exit code 3 before signing a
link. Authorization is not added to the tool arguments.

The payload requires `columns` and a nonempty detail `slice`. Aggregate filters
stay in `having`; the CLI does not convert detail filters or change the model's
query semantics. JSON output preserves the server's `viewerUrl`,
`viewerLinkExpiresAt`, `expiresAt`, and `queryExpiresAt` when returned. Use global
`--output pretty` to print only the link, then open it in a browser.

Link lifetime and browser-session lifetime are separate configurable server
policies. The CLI does not fabricate links or pass a TTL override. A null link
expiry is preserved when the server is configured for permanent links; session
expiry is still enforced by the server. Treat generated links as private access
links when recording demonstrations or saving output.

### Export a query image

`query export-image` calls the advertised `dataset.export_image` MCP tool. The
Java Runtime executes the semantic query with the same namespace and model
permissions, then renders a table, bar chart or line chart as PNG inside the JVM.
No Docker, browser, Node service or local plotting package is needed. Python
Runtime support is outside this initial feature.

```powershell
foggy --base-url http://127.0.0.1:18066 --namespace demo `
  query export-image SalesQueryModel --payload .\monthly-sales.json `
  --kind bar --x month --y totalAmount --title "月度销售额" --unit "元" `
  --out .\sales.png

foggy --base-url http://127.0.0.1:18066 --namespace demo `
  query export-image SalesQueryModel --payload .\monthly-sales.json `
  --kind table --title "月度销售明细" --columns .\image-columns.json `
  --out .\sales-table.png
```

Use result field names from the described model/query output; chart fields are
explicit `--x` and `--y` arguments. The CLI preserves the query DSL and never
guesses a field, changes aggregation or reorders the data. Keep using
`models describe` and `query validate` when preparing the payload.

The optional `--columns` file is a UTF-8 JSON array:

```json
[
  {"field": "month", "caption": "月份"},
  {"field": "totalAmount", "caption": "销售额", "unit": "元"}
]
```

The default kind is `table`. The Runtime chooses captions from query metadata
when no override is supplied and loads its bundled Chinese font. `--x-label`,
`--y-label` and `--unit` override chart labels without changing values. Width
defaults to 1200 pixels (`--width` accepts 640..2400); optional `--height` accepts
360..2400 for tables or 520..2400 for bar and line charts. Tables size their height
to their content. `--max-rows` accepts 1..50
for tables, 1..40 for bars or 1..100 for lines; omitted limits use those Runtime defaults.
Tables support up to 12 columns, and omitted rows are visibly labelled.
The Runtime validates label lengths and layout before rendering: title up to
120 characters, field names and table captions up to 100, chart axis labels up
to 80, and units up to 32. A length or layout error is returned without saving
an image; shorten the explicit label rather than changing query values.

The output directory must already exist. The CLI requests inline PNG content,
checks strict base64, PNG chunk boundaries and CRCs, required image data and
chunk ordering, dimensions and an 8 MiB size limit, then atomically replaces `--out`.
A failed request, invalid image or failed save leaves an existing output file
unchanged. JSON output contains the saved absolute path, dimensions and size;
it never includes the base64 PNG, connection parameters or credentials.
Images contain the selected query data, so share them with the intended audience.

Authorization uses the same global option or environment variable as ordinary
queries and is sent only in the MCP HTTP header. The default MCP endpoint and
`--mcp-path` options match `query open-in-viewer`. The CLI never downloads an
image URL or forwards credentials to an image host. Missing tool support exits
with code 3; query/tool errors use 2, transport failures 4, and local input,
image-format or file-write errors 1.

Maintainer validation on 2026-10-03 used the checkout's actual source rather
than an installed CLI package:

```powershell
python -m pytest tests/test_image_cli.py tests/test_viewer_cli.py `
  tests/test_client_http.py tests/test_cli.py --override-ini pythonpath=src -q
```

The run passed 145 tests and 58 subtests. It covers HTTP namespace and opaque
Authorization propagation, MCP tool discovery and errors, native image and
structured/text response handling, invalid PNG/base64, damaged chunk CRCs,
truncated or misordered chunks and size limits, height
and row limits, credential redaction, and preservation of existing files after
request, format or atomic-write failures. PNG base64 remains intact when a
short credential coincidentally occurs in verified binary image content;
malformed or disguised content still receives normal credential redaction.

These CLI checks validate transport and file delivery. Rendering style,
Chinese glyphs, actual query values and JVM resource use require the Java
renderer and semantic-query integration checks. The CLI adds no rendering
implementation or Python dependencies. Multi-result Compose and complex
report design are outside this initial command. The command requires an MCP
endpoint that advertises `dataset.export_image` and returns inline PNG;
URL-only image responses are intentionally unsupported.

`query explain` is an evidence-only command for requests that explicitly ask how a result was produced, how a field or metric maps from QM to TM/physical SQL, or how permissions and pre-aggregation affect a query. Omit `--payload` for `DEFINITION`; provide a semantic query payload for `RECOMPILED`. It does not return business result rows and must not replace ordinary queries, model discovery, or semantic loading.

Use `wait-ready` after starting a local dev/test runtime. It polls `GET /api/v1/capabilities` until the Runtime API is reachable and returns success; transient transport failures are retained in JSON `data.attempts`.

Use `capabilities` to inspect the connected runtime's engine, Runtime API version, schema version, security mode, and supported capability map.

For human diagnostics, `--output pretty capabilities` prints a compact runtime summary:

```text
engine: java
runtimeApiVersion: foggy-runtime-api/v1
schemaVersion: 2026-06-06
enabled: true
securityMode: none-dev-test-only
capabilities:
  models.refresh: supported
  query.validate: supported
```

Automation and Skills should keep using JSON output so they can validate the full envelope and diagnostics.

`models validate` sends `clearExisting=true` by default so repeated validation runs replace the temporary runtime validation bundle. Use `--no-clear-existing` only when debugging bundle watch behavior.

`bundles list|add|update|remove` manages only Runtime API-owned bundles. Configured bundles may appear in `bundles list`, but the runtime rejects update/remove for bundles that came from yml, startup args, or other engine configuration.

`bundles update` sends `watch` only when `--watch` or `--no-watch` is provided, so an update that changes only the path does not accidentally change the runtime watch setting. `bundles remove` maps to `DELETE /api/v1/bundles/{name}` without a request body.

`datasources binding --namespace <namespace>` reads the namespace datasource binding with `GET /api/v1/namespaces/{namespace}/datasource`; `datasources bind` updates it with `PUT`. `datasources diagnostics` calls `GET /api/v1/datasources/diagnostics` and is the preferred evidence command for managed datasource registry path, namespace bindings, persisted records, and pool lifecycle state when the runtime supports `datasources.diagnostics`.

`tables inspect` sends `includeForeignKeys` only when `--include-foreign-keys` or `--no-foreign-keys` is explicitly provided. Use `--include-indexes` when index metadata is needed.

`resources pull|save` syncs `.tm`, `.qm`, and model-list files for a named filesystem bundle. Save is allowed only for Runtime API-owned bundles. The current Runtime API accepts `--validate` and `--refresh` on save but returns warnings; run `models validate` and `models refresh` explicitly when evidence is needed.

Commands that require Runtime API features preflight `capabilities` and stop with exit code `3` when the connected runtime does not support the required capability. This includes models, query, table inspection, SQL probing, bundle/datasource/resource management, compose, and fsscript commands.

`compose validate|preview|execute` and `fsscript run` read `--script <path>` or `--script -`; use `--script-text` only for short inline smoke checks.

`fsscript run` does not expose `foggy.cte.*` by default. Use `--enable-cte-bridge` only for dev/test Runtime API sessions where `fsscript.cteBridge` is supported and the script intentionally calls restricted Compose/CTE through the host-injected bridge.

When validating copied fixtures, confirm the runtime datasource first. For the Java `lite` profile, use `docs/v4.1/contracts/runtime-api-v1/model-fixtures/minimal-fact-order` as the default smoke fixture; the broader ecommerce demo directory requires a fuller schema and is expected to fail under lite.

## Local Demo Planning

Before starting the full local demo replay, run the CLI plan helper:

```powershell
foggy-runtime demo sales-drop plan `
  --repo-root D:\foggy-projects\foggy-data-mcp `
  --port 18066 `
  --namespace salesdrop
```

The plan helper verifies the bundled sales-drop Skill assets and emits the local command plan. It does not start Java.

`demo sales-drop plan` is a local planning helper for the `foggy-ai-analysis` Skill. It does not call Runtime API endpoints or Java/Python private routes. It verifies that the bundled sales-drop Skill assets exist, then emits a JSON command plan for:

- SQLite schema/data seeding.
- Java lite runtime startup.
- `wait-ready`.
- `capabilities`.
- `tables inspect`.
- `models validate`.
- `models refresh`.
- `models describe`.
- `query validate`.
- `query execute`.

If the launcher JAR is missing, the command still returns a plan with a warning so a clean workspace can tell the user to build `foggy-mcp-launcher` or pass `--launcher-jar`.

By default, the helper looks under `~/.agents/skills/foggy-ai-analysis`, then workspace development copies. Use `--skill-dir` when the Skill was unpacked outside those locations. The plan output includes both `skillDir` and `demoDir` so automation can verify which asset copy is being used.

## No-Workspace Sales-Drop Replay

`demo sales-drop replay` is a public-onboarding helper for an already running Runtime API. It does not require a `foggy-runtime-cli` source checkout or the workspace PowerShell replay script.

Inputs:

- A running Java lite Runtime API, usually `http://127.0.0.1:18066`.
- An installed or unpacked `foggy-ai-analysis` Skill directory from the release zip.
- The same SQLite file path used by the running runtime default datasource when `--use-default-datasource` is set.
- Optional `--evidence-dir`; otherwise evidence is written under `.foggy-demo/sales-drop-replay-<stamp>`.

Example:

```powershell
foggy-runtime --base-url http://127.0.0.1:18066 demo sales-drop replay `
  --skill-dir D:\demo\skills\foggy-ai-analysis `
  --evidence-dir D:\demo\evidence\sales-drop-replay `
  --sqlite-path D:\demo\runtime\sales_drop_demo.sqlite `
  --use-default-datasource
```

For the public sales-drop replay, use `--use-default-datasource` and start Java with the same SQLite file:

```powershell
java -Dfile.encoding=UTF-8 -jar foggy-runtime-launcher-0.1.24.jar `
  --server.port=18066 `
  --spring.profiles.active=lite `
  --foggy.runtime-api.enabled=true `
  --spring.datasource.url=jdbc:sqlite:D:\demo\runtime\sales_drop_demo.sqlite
```

This mode seeds the bundled SQLite fixture into the runtime default datasource, tests that datasource, inspects the table, runs a read-only SQL sample, validates and registers the bundled TM/QM bundle, refreshes and describes `SalesDropDailyQueryModel`, executes the basic query, and replays the bundled question bank.

The command also supports Runtime API-managed datasource registration without `--use-default-datasource`. Keep that path for user-owned databases and namespace-bound model/query smoke tests; keep `--use-default-datasource` for the sales-drop replay so demo seeding never writes to a user database.

Evidence files:

- `summary.json`
- `command-status.json`
- `question-bank-replay.json`
- `cli-sales-drop-replay-report.md`
- `logs\*.json`

The command expects the Runtime API to report `securityMode=none-dev-test-only`. It is for trusted local dev/test onboarding. It does not download or start Java yet; keep using the released Java launcher scripts or the maintainer workspace replay when validating full install-state lifecycle.

## Feedback

File CLI install, packaging, command behavior, exit code, or Runtime API client issues at:

```text
https://github.com/foggy-projects/foggy-runtime-cli/issues/new/choose
```

For demo replay failures, include the release versions, runtime URL, namespace, command status CSV, `summary.json`, and any sanitized CLI stdout/stderr logs.
