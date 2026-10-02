# Foggy Runtime CLI 0.1.24

Adds `foggy-runtime query open-in-viewer <model> --payload <file>` to generate a
data-view link through the existing MCP `dataset.open_in_viewer` tool. The command
uses the configured Authorization header and namespace, verifies tool availability
before signing, preserves detail `slice` and aggregate `having`, and returns the
server-issued URL and expiry fields. No separate viewer login is required.

The configured server controls link lifetime and browser-session lifetime. This
CLI does not invent URLs, convert filters, or override TTL. Supported MCP paths
are `/mcp/analyst/rpc` (default), `/mcp/analyst`, `/mcp/business/rpc`, and
`/mcp/business`; use `--mcp-path` to match the existing connection.

Also includes canonical paired Skill discovery and updates the verified offline
stack recommendation to the verified Launcher 0.1.24 and Skill 0.1.18 releases. Existing Runtime API
query and model commands keep their contracts.

Assets include the wheel, source distribution, Windows and POSIX installers,
`release-manifest.json`, and `SHA256SUMS`. Every release asset except the checksum
file itself is covered by SHA256SUMS. GitHub is the authoritative release source.
