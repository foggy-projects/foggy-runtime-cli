#!/usr/bin/env python3
"""Verify wheel/sdist-installed CLI delivery against a packaged Java MCP fixture.

Build outputs, isolated environments and receipts stay under the chosen output
directory. Credentials come from environment variables and are never recorded.
This does not install or replace the global foggy command or start Java.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PIP_USER"):
        environment.pop(name, None)
    return environment


def run_process(command: list[str], environment: dict[str, str], cwd: Path,
                phase: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=environment, text=True,
                            encoding="utf-8", errors="replace", capture_output=True, timeout=timeout)
    if phase and result.returncode:
        # Build/install output can include machine paths or index credentials;
        # receipts retain the failed phase and exit code, never that raw output.
        raise RuntimeError(f"{phase} failed with exit code {result.returncode}")
    return result


def python_in(environment_dir: Path) -> Path:
    return environment_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def foggy_in(environment_dir: Path) -> Path:
    return environment_dir / ("Scripts/foggy.exe" if os.name == "nt" else "bin/foggy")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UnsupportedRuntimeHandler(BaseHTTPRequestHandler):
    methods: list[str] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).methods.append(body.get("method", ""))
        response = {"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}}
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args: Any) -> None:
        pass


@contextmanager
def unsupported_runtime():
    UnsupportedRuntimeHandler.methods = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), UnsupportedRuntimeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def check_stdout(result: subprocess.CompletedProcess[str], forbidden: list[str]) -> dict[str, Any]:
    for value in forbidden:
        if value and (value in result.stdout or value in result.stderr):
            raise RuntimeError("CLI response exposed a credential or configured Runtime URL")
    if result.stderr.strip():
        raise RuntimeError("CLI wrote unexpected diagnostic output outside its JSON response")
    try:
        response = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Installed CLI stdout is not one machine-readable JSON response") from exc
    if not isinstance(response, dict):
        raise RuntimeError("Installed CLI did not return a JSON object")
    if "iVBORw0KGgo" in result.stdout:
        raise RuntimeError("Installed CLI stdout included inline PNG data")
    return response


def install_package(args: argparse.Namespace, archive: Path, kind: str,
                    environment: dict[str, str]) -> tuple[Path, dict[str, Any]]:
    environment_dir = args.out / kind / "venv"
    if environment_dir.exists() and not args.reuse_environments:
        raise RuntimeError(f"{kind} acceptance environment already exists; choose a fresh --out directory")
    if not environment_dir.exists():
        run_process([sys.executable, "-m", "venv", str(environment_dir)], environment, args.project,
                    f"{kind}.venv", timeout=180)
    installed_environment = environment.copy()
    installed_environment["PYTHONNOUSERSITE"] = "1"
    package_python = python_in(environment_dir)
    if not args.reuse_environments:
        run_process([str(package_python), "-m", "pip", "--isolated", "--disable-pip-version-check",
                     "install", "--no-deps", str(archive)], installed_environment,
                    args.out, f"{kind}.install", timeout=300)
    probe_code = (
        "import importlib.metadata as m,json; import foggy_runtime_cli.image_cli as image; "
        "d=m.distribution('foggy-runtime-cli'); origin=json.loads(d.read_text('direct_url.json') or '{}'); "
        "print(json.dumps({'version':d.version,'module':image.__file__,"
        "'archiveSha256':origin.get('archive_info',{}).get('hashes',{}).get('sha256'),"
        "'dependencies':d.requires or [],'entryPoints':sorted(e.name for e in d.entry_points if e.group=='console_scripts')}))"
    )
    probe = run_process([str(package_python), "-c", probe_code], installed_environment,
                        args.out, f"{kind}.probe")
    data = json.loads(probe.stdout)
    module_path = Path(data.pop("module")).resolve()
    if not module_path.is_relative_to(environment_dir.resolve()):
        raise RuntimeError(f"{kind} imported CLI outside its isolated installed environment")
    if sha256(module_path) != sha256(args.project / "src/foggy_runtime_cli/image_cli.py"):
        raise RuntimeError(f"{kind} installed image command does not match the source being accepted")
    if data["archiveSha256"] != sha256(archive):
        raise RuntimeError(f"{kind} installed archive does not match the acceptance artifact")
    data["module"] = module_path.relative_to(environment_dir.resolve()).as_posix()
    data["pythonPathCleared"] = "PYTHONPATH" not in installed_environment
    data["imageModuleSha256"] = sha256(module_path)
    if data["dependencies"] or not {"foggy", "foggy-runtime"}.issubset(data["entryPoints"]):
        raise RuntimeError(f"{kind} package dependency or entry-point contract changed")
    return foggy_in(environment_dir), data


def verify_installed(args: argparse.Namespace, foggy: Path, package_kind: str,
                     base_environment: dict[str, str]) -> list[dict[str, Any]]:
    environment = base_environment.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    authorization = os.environ.get(args.authorization_env)
    denied_authorization = os.environ.get(args.denied_authorization_env)
    if not authorization or not denied_authorization or authorization == denied_authorization:
        raise RuntimeError("Set distinct fixture authorization and denied-authorization environment values")
    environment["FOGGY_RUNTIME_AUTHORIZATION"] = authorization
    environment.pop("FOGGY_RUNTIME_API_AUTH_CODE", None)
    forbidden = [authorization, denied_authorization, args.base_url]
    folder = args.out / package_kind
    common = [str(foggy), "--base-url", args.base_url, "--namespace", args.namespace,
              "query", "export-image", args.model, "--payload", str(args.payload), "--mcp-path", args.mcp_path]
    records: list[dict[str, Any]] = []
    for kind in ("table", "bar", "line"):
        destination = folder / f"{kind}.png"
        command = common + ["--kind", kind, "--out", str(destination), "--title", args.title]
        if kind != "table":
            command += ["--x", args.x, "--y", args.y]
        started = time.monotonic()
        result = run_process(command, environment, args.out, "")
        response = check_stdout(result, forbidden)
        if result.returncode or response.get("success") is not True:
            error = response.get("error", {})
            raise RuntimeError(f"{package_kind}.{kind} failed: exit={result.returncode}, code={error.get('code', 'unknown')}")
        data = response.get("data", {})
        png = destination.read_bytes()
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or data.get("bytes") != len(png):
            raise RuntimeError(f"{package_kind}.{kind} did not deliver the announced PNG")
        if Path(data.get("file", "")).resolve() != destination.resolve():
            raise RuntimeError(f"{package_kind}.{kind} did not save the requested output file")
        records.append({"case": kind, "exitCode": result.returncode, "image": f"{package_kind}/{kind}.png",
                        "sha256": sha256(destination), "elapsedMillis": round((time.monotonic() - started) * 1000),
                        **{key: data[key] for key in ("mimeType", "bytes", "width", "height", "rowsRendered", "rowsReturned", "truncated") if key in data}})
    if args.secondary_namespace:
        destination = folder / "bar-secondary.png"
        command = [str(foggy), "--base-url", args.base_url, "--namespace", args.secondary_namespace,
                   "query", "export-image", args.model, "--payload", str(args.payload), "--mcp-path", args.mcp_path,
                   "--kind", "bar", "--x", args.x, "--y", args.y, "--title", args.title, "--out", str(destination)]
        result = run_process(command, environment, args.out, "")
        response = check_stdout(result, forbidden)
        if result.returncode or response.get("success") is not True or sha256(destination) == sha256(folder / "bar.png"):
            raise RuntimeError(f"{package_kind}.secondary-namespace did not produce distinct fixture data")
        records.append({"case": "namespace-isolation", "exitCode": 0,
                        "image": f"{package_kind}/bar-secondary.png", "sha256": sha256(destination),
                        "differentFromPrimary": True})
    marker = b"preserve-existing-file-on-failure"
    negative = folder / "preserve-existing.png"
    cases = [("invalid-field", environment, common + ["--kind", "bar", "--x", "unknown_fixture_field", "--y", args.y], 2)]
    denied_environment = environment.copy()
    denied_environment["FOGGY_RUNTIME_AUTHORIZATION"] = denied_authorization
    cases.append(("permission-denied", denied_environment, common + ["--kind", "table"], 2))
    for case, case_environment, command, expected in cases:
        negative.write_bytes(marker)
        result = run_process(command + ["--out", str(negative)], case_environment, args.out, "")
        response = check_stdout(result, forbidden)
        if result.returncode != expected or response.get("success") is not False or negative.read_bytes() != marker:
            raise RuntimeError(f"{package_kind}.{case} did not reject safely and preserve the existing file")
        records.append({"case": case, "exitCode": result.returncode, "errorCode": response.get("error", {}).get("code"),
                        "existingFilePreserved": True})
    with unsupported_runtime() as url:
        negative.write_bytes(marker)
        command = [str(foggy), "--base-url", url, "--namespace", args.namespace, "query", "export-image", args.model,
                   "--payload", str(args.payload), "--kind", "table", "--out", str(negative)]
        result = run_process(command, environment, args.out, "")
        response = check_stdout(result, forbidden + [url])
        if (result.returncode != 3 or response.get("success") is not False or negative.read_bytes() != marker
                or UnsupportedRuntimeHandler.methods != ["tools/list"]):
            raise RuntimeError(f"{package_kind}.older-runtime did not stop at discovery and preserve the file")
        records.append({"case": "older-runtime-no-tool", "exitCode": 3,
                        "errorCode": response.get("error", {}).get("code"), "existingFilePreserved": True, "calls": ["tools/list"]})
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True, help="Fresh output directory (normally under ignored build/).")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--secondary-namespace", help="Optional fixture namespace with the same model but different data.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--x", required=True)
    parser.add_argument("--y", required=True)
    parser.add_argument("--title", default="月度销售额")
    parser.add_argument("--mcp-path", default="/mcp/analyst/rpc")
    parser.add_argument("--authorization-env", default="FOGGY_RUNTIME_AUTHORIZATION")
    parser.add_argument("--denied-authorization-env", default="FOGGY_IMAGE_DENIED_AUTHORIZATION")
    parser.add_argument("--skip-build", action="store_true", help="Use artifacts already built into --out/packages; source hash is still verified.")
    parser.add_argument("--prepare-only", action="store_true", help="Build/install/probe both packages without calling MCP yet.")
    parser.add_argument("--reuse-environments", action="store_true", help="Reuse prepared environments after verifying archive and installed source hashes.")
    args = parser.parse_args()
    args.project, args.out, args.payload = args.project.resolve(), args.out.resolve(), args.payload.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    receipt: dict[str, Any] = {"success": False, "timestamp": datetime.now(timezone.utc).isoformat(),
                               "scope": "installed wheel/sdist CLI -> packaged Java MCP fixture", "packages": []}
    receipt["prepareOnly"] = args.prepare_only
    environment = clean_environment()
    build_environment = environment.copy()
    for name in (args.authorization_env, args.denied_authorization_env, "FOGGY_RUNTIME_AUTHORIZATION", "FOGGY_RUNTIME_API_AUTH_CODE"):
        build_environment.pop(name, None)
    try:
        archives = args.out / "packages"
        if not args.skip_build:
            print("Building wheel and sdist with isolated PEP 517 environments", flush=True)
            run_process([sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(archives)],
                        build_environment, args.project, "package.build", timeout=300)
        for kind, pattern in (("wheel", "*.whl"), ("sdist", "*.tar.gz")):
            matches = list(archives.glob(pattern))
            if len(matches) != 1:
                raise RuntimeError(f"Expected one {kind} artifact")
            archive = matches[0]
            action = "Validating prepared" if args.reuse_environments else "Installing and validating new"
            print(f"{action} {kind} environment", flush=True)
            foggy, installed = install_package(args, archive, kind, build_environment)
            package = {"kind": kind, "artifact": f"packages/{archive.name}", "bytes": archive.stat().st_size, "sha256": sha256(archive),
                       "installed": installed, "cases": [] if args.prepare_only else verify_installed(args, foggy, kind, environment)}
            receipt["packages"].append(package)
        receipt["success"] = True
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        # Exception messages intentionally contain phases/codes only, never
        # subprocess output, headers, query bodies or configured server URLs.
        receipt["error"] = {"code": type(exc).__name__, "message": str(exc) if isinstance(exc, RuntimeError) else "Acceptance operation failed"}
    (args.out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": receipt["success"], "packagesValidated": len(receipt["packages"]),
                      "prepareOnly": args.prepare_only, "receipt": "receipt.json",
                      **({"error": receipt["error"]} if "error" in receipt else {})}, ensure_ascii=False))
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
