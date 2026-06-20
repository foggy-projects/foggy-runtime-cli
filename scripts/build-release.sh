#!/usr/bin/env bash
set -euo pipefail

clean=0
skip_tests=0
skip_install=0
skip_venv=0

for arg in "$@"; do
  case "$arg" in
    --clean) clean=1 ;;
    --skip-tests) skip_tests=1 ;;
    --skip-install) skip_install=1 ;;
    --skip-venv) skip_venv=1 ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dist_dir="$project_root/dist"
pyproject="$project_root/pyproject.toml"
version_file="$project_root/src/foggy_runtime_cli/__init__.py"
manifest="$dist_dir/release-manifest.json"
checksums="$dist_dir/SHA256SUMS"
release_venv="$project_root/.release-venv"
python_exe="python"

read_pyproject_version() {
  sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$pyproject" | head -n 1
}

read_package_version() {
  sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$version_file" | head -n 1
}

project_version="$(read_pyproject_version)"
package_version="$(read_package_version)"

if [[ -z "$project_version" || -z "$package_version" ]]; then
  echo "Could not read package versions." >&2
  exit 1
fi

if [[ "$project_version" != "$package_version" ]]; then
  echo "Version mismatch: pyproject.toml=$project_version, __init__.py=$package_version" >&2
  exit 1
fi

cd "$project_root"

if [[ "$clean" == "1" ]]; then
  rm -rf "$dist_dir"
fi
mkdir -p "$dist_dir"

if [[ "$skip_venv" == "0" ]]; then
  if [[ ! -d "$release_venv" ]]; then
    python -m venv "$release_venv"
  fi
  python_exe="$release_venv/bin/python"
fi

if [[ "$skip_install" == "0" ]]; then
  "$python_exe" -m pip install --upgrade pip build pytest
fi

if [[ "$skip_tests" == "0" ]]; then
  PYTHONPATH="$project_root/src${PYTHONPATH:+:$PYTHONPATH}" "$python_exe" -m pytest tests
fi

"$python_exe" -m build --sdist --wheel

artifacts=()
while IFS= read -r artifact; do
  artifacts+=("$(basename "$artifact")")
done < <(find "$dist_dir" -maxdepth 1 -type f \( -name '*.whl' -o -name '*.tar.gz' \) | sort)
if [[ "${#artifacts[@]}" == "0" ]]; then
  echo "No wheel or sdist artifacts were created in $dist_dir" >&2
  exit 1
fi

: > "$checksums"
for artifact in "${artifacts[@]}"; do
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$dist_dir/$artifact" | sed "s#$dist_dir/##" >> "$checksums"
  else
    shasum -a 256 "$dist_dir/$artifact" | sed "s#$dist_dir/##" >> "$checksums"
  fi
done

python - "$project_version" "$dist_dir" "$manifest" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

version = sys.argv[1]
dist = Path(sys.argv[2])
manifest = Path(sys.argv[3])
artifacts = []
for path in sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")]):
    artifacts.append({
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    })

manifest.write_text(json.dumps({
    "schemaVersion": "foggy-runtime-cli-release/v1",
    "version": version,
    "generatedAt": datetime.now(timezone.utc).isoformat(),
    "artifacts": artifacts,
    "checksums": "SHA256SUMS",
}, indent=2) + "\n", encoding="utf-8")
PY

echo "Release package ready."
echo "Version: $project_version"
echo "Dist: $dist_dir"
cat "$checksums"
