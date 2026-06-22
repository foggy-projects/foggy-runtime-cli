# foggy-runtime-cli

Standalone CLI for `Foggy Runtime API v1`.

The CLI talks only to `/api/v1/*` runtime endpoints. It does not call Java or Python engine private routes.

## Installation

Windows PowerShell from GitHub Release:

```powershell
$version = "0.1.4"
$download = Join-Path $env:TEMP "foggy-runtime-cli-install-$version"
New-Item -ItemType Directory -Force -Path $download | Out-Null
Invoke-WebRequest `
  -Uri "https://github.com/foggy-projects/foggy-runtime-cli/releases/download/v$version/install-foggy-runtime-cli.ps1" `
  -OutFile (Join-Path $download "install-foggy-runtime-cli.ps1")
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $download "install-foggy-runtime-cli.ps1") -Version $version
foggy-runtime --help
python -m pip show foggy-runtime-cli
```

Linux/macOS from GitHub Release:

```bash
version="0.1.4"
download="${TMPDIR:-/tmp}/foggy-runtime-cli-install-$version"
mkdir -p "$download"
curl -fsSL "https://github.com/foggy-projects/foggy-runtime-cli/releases/download/v$version/install-foggy-runtime-cli.sh" -o "$download/install-foggy-runtime-cli.sh"
bash "$download/install-foggy-runtime-cli.sh" --version "$version"
foggy-runtime --help
python -m pip show foggy-runtime-cli
```

From a released wheel:

```powershell
python -m pip install foggy_runtime_cli-0.1.4-py3-none-any.whl
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

## Release Packaging

Build local release artifacts:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1 -Clean
```

Linux/macOS:

```bash
bash scripts/build-release.sh --clean
```

The release build runs tests by default, builds wheel and sdist artifacts into `dist/`, then writes `dist/SHA256SUMS` and `dist/release-manifest.json`.

GitHub releases are created from tags by `.github/workflows/release.yml`:

```powershell
git tag -a v0.1.4 -m "Release v0.1.4"
git push origin v0.1.4
```

Release assets include:

- `foggy_runtime_cli-<version>-py3-none-any.whl`
- `foggy_runtime_cli-<version>.tar.gz`
- `SHA256SUMS`
- `release-manifest.json`
- `install-foggy-runtime-cli.ps1`
- `install-foggy-runtime-cli.sh`

For `v0.1.2` and later, the public CLI release can also carry companion `foggy-ai-analysis-demo` Skill assets uploaded from the workspace packaging script:

- `foggy-ai-analysis-demo-skill-<version>.zip`
- `foggy-ai-analysis-demo-skill-<version>-manifest.json`
- `foggy-ai-analysis-demo-skill-<version>-SHA256SUMS`

## Examples

```powershell
foggy-runtime --base-url http://127.0.0.1:8080 capabilities
foggy-runtime --base-url http://127.0.0.1:8080 wait-ready --timeout-seconds 90 --interval-seconds 2
foggy-runtime bundles list
foggy-runtime bundles add --name sales-drop-dev --path ./models --namespace default --watch --validate --refresh
foggy-runtime bundles update sales-drop-dev --path ./models --watch
foggy-runtime bundles remove sales-drop-dev
foggy-runtime resources pull --bundle sales-drop-dev --out ./work-models
foggy-runtime resources save --bundle sales-drop-dev --dir ./work-models --validate --refresh
foggy-runtime models list
foggy-runtime models describe FactSalesQueryModel
foggy-runtime models refresh --model FactSalesQueryModel
foggy-runtime models validate --models-dir ./models
foggy-runtime query validate FactSalesQueryModel --payload query.json
foggy-runtime query execute FactSalesQueryModel --payload -
foggy-runtime compose validate --script compose.fsscript
foggy-runtime compose preview --script compose.fsscript
foggy-runtime compose execute --script compose.fsscript
foggy-runtime fsscript run --script workflow.fsscript
foggy-runtime fsscript run --script workflow.fsscript --enable-cte-bridge
foggy-runtime tables inspect --table sale_order --schema public --include-indexes
foggy-runtime demo sales-drop plan --repo-root D:\foggy-projects\foggy-data-mcp --port 18066
foggy-runtime demo sales-drop plan --repo-root D:\foggy-projects\foggy-data-mcp --skill-dir D:\demo\skills\foggy-ai-analysis-demo --port 18066
foggy-runtime --base-url http://127.0.0.1:18066 demo sales-drop replay --skill-dir D:\demo\skills\foggy-ai-analysis-demo --evidence-dir D:\demo\evidence --sqlite-path D:\demo\runtime\sales_drop_demo.sqlite --use-default-datasource
```

JSON output is the default and preserves the Runtime API envelope for Skill consumption.

The CLI is backend-neutral and does not select Java or Python. `--base-url` always wins, followed by `FOGGY_RUNTIME_API_URL`, then the local development default `http://127.0.0.1:8080`.

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

`resources pull|save` syncs `.tm`, `.qm`, and model-list files for a named filesystem bundle. Save is allowed only for Runtime API-owned bundles. The current Runtime API accepts `--validate` and `--refresh` on save but returns warnings; run `models validate` and `models refresh` explicitly when evidence is needed.

`compose validate|preview|execute` and `fsscript run` read `--script <path>` or `--script -`; use `--script-text` only for short inline smoke checks. These commands preflight `capabilities` and stop with exit code `3` when the connected Runtime API does not support the required capability.

`fsscript run` does not expose `foggy.cte.*` by default. Use `--enable-cte-bridge` only for dev/test Runtime API sessions where `fsscript.cteBridge` is supported and the script intentionally calls restricted Compose/CTE through the host-injected bridge.

When validating copied fixtures, confirm the runtime datasource first. For the Java `lite` profile, use `docs/v4.1/contracts/runtime-api-v1/model-fixtures/minimal-fact-order` as the default smoke fixture; the broader ecommerce demo directory requires a fuller schema and is expected to fail under lite.

## Local Demo Planning

Before starting the full local demo replay, run the preflight:

```powershell
powershell -ExecutionPolicy Bypass -File foggy-runtime-cli\scripts\verify-foggy-ai-analysis-demo.ps1 `
  -RepoRoot D:\foggy-projects\foggy-data-mcp `
  -Port 18066 `
  -Namespace salesdrop
```

The preflight checks Java/Python availability, source-layout CLI import, bundled Skill assets, optional Skill validation, launcher JAR presence, port availability, and the local sales-drop command plan. It does not start Java.

`demo sales-drop plan` is a local planning helper for the `foggy-ai-analysis-demo` skill. It does not call Runtime API endpoints or Java/Python private routes. It verifies that the bundled sales-drop skill assets exist, then emits a JSON command plan for:

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

Use `--skill-dir` when the Skill was downloaded from a release zip and unpacked outside the workspace `.codex\skills` directory. The plan output includes both `skillDir` and `demoDir` so automation can verify which asset copy is being used.

## No-Workspace Sales-Drop Replay

`demo sales-drop replay` is a public-onboarding helper for an already running Runtime API. It does not require a `foggy-runtime-cli` source checkout or the workspace PowerShell replay script.

Inputs:

- A running Java lite Runtime API, usually `http://127.0.0.1:18066`.
- An unpacked `foggy-ai-analysis-demo` Skill directory from the release zip.
- The same SQLite file path used by the running runtime default datasource when `--use-default-datasource` is set.
- Optional `--evidence-dir`; otherwise evidence is written under `.foggy-demo/sales-drop-replay-<stamp>`.

Example:

```powershell
foggy-runtime --base-url http://127.0.0.1:18066 demo sales-drop replay `
  --skill-dir D:\demo\skills\foggy-ai-analysis-demo `
  --evidence-dir D:\demo\evidence\sales-drop-replay `
  --sqlite-path D:\demo\runtime\sales_drop_demo.sqlite `
  --use-default-datasource
```

With current Java runtimes, use `--use-default-datasource` and start Java with the same SQLite file:

```powershell
java -Dfile.encoding=UTF-8 -jar foggy-mcp-launcher-9.1.0.beta-runtime-api.jar `
  --server.port=18066 `
  --spring.profiles.active=lite `
  --foggy.runtime-api.enabled=true `
  --spring.datasource.url=jdbc:sqlite:D:\demo\runtime\sales_drop_demo.sqlite
```

This mode seeds the bundled SQLite fixture into the runtime default datasource, tests that datasource, inspects the table, runs a read-only SQL sample, validates and registers the bundled TM/QM bundle, refreshes and describes `SalesDropDailyQueryModel`, executes the basic query, and replays the bundled question bank.

The command also supports Runtime API-managed datasource registration without `--use-default-datasource`. That path is useful for table/SQL exploration, but current Java model validation still reads the runtime default datasource; keep the default datasource mode for end-to-end sales-drop replay until the Runtime API model/query layer consumes namespace datasource bindings.

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
