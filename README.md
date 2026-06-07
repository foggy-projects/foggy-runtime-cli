# foggy-runtime-cli

Standalone CLI for `Foggy Runtime API v1`.

The CLI talks only to `/api/v1/*` runtime endpoints. It does not call Java or Python engine private routes.

## Examples

```powershell
foggy-runtime --base-url http://127.0.0.1:8080 capabilities
foggy-runtime --engine python capabilities
foggy-runtime models list
foggy-runtime models describe FactSalesQueryModel
foggy-runtime models refresh --model FactSalesQueryModel
foggy-runtime models validate --models-dir ./models
foggy-runtime query validate FactSalesQueryModel --payload query.json
foggy-runtime query execute FactSalesQueryModel --payload -
foggy-runtime tables inspect --table sale_order --schema public --include-indexes
```

JSON output is the default and preserves the Runtime API envelope for Skill consumption.

`--engine` selects only a base-url profile. `java` defaults to `http://127.0.0.1:8080`, and `python` defaults to `http://127.0.0.1:8066`. `--base-url` always wins, followed by `FOGGY_RUNTIME_API_URL`, then `FOGGY_JAVA_RUNTIME_API_URL` or `FOGGY_PYTHON_RUNTIME_API_URL`.

`models validate` sends `clearExisting=true` by default so repeated validation runs replace the temporary runtime validation bundle. Use `--no-clear-existing` only when debugging bundle watch behavior.

When validating copied fixtures, confirm the runtime datasource first. For the Java `lite` profile, use `docs/v4.1/contracts/runtime-api-v1/model-fixtures/minimal-fact-order` as the default smoke fixture; the broader ecommerce demo directory requires a fuller schema and is expected to fail under lite.
