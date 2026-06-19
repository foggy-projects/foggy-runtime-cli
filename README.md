# foggy-runtime-cli

Standalone CLI for `Foggy Runtime API v1`.

The CLI talks only to `/api/v1/*` runtime endpoints. It does not call Java or Python engine private routes.

## Examples

```powershell
foggy-runtime --base-url http://127.0.0.1:8080 capabilities
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
```

JSON output is the default and preserves the Runtime API envelope for Skill consumption.

The CLI is backend-neutral and does not select Java or Python. `--base-url` always wins, followed by `FOGGY_RUNTIME_API_URL`, then the local development default `http://127.0.0.1:8080`.

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

`demo sales-drop plan` is a local planning helper for the `foggy-ai-analysis-demo` skill. It does not call Runtime API endpoints or Java/Python private routes. It verifies that the bundled sales-drop skill assets exist, then emits a JSON command plan for:

- SQLite schema/data seeding.
- Java lite runtime startup.
- `capabilities`.
- `tables inspect`.
- `models validate`.
- `models refresh`.
- `models describe`.
- `query validate`.
- `query execute`.

If the launcher JAR is missing, the command still returns a plan with a warning so a clean workspace can tell the user to build `foggy-mcp-launcher` or pass `--launcher-jar`.
