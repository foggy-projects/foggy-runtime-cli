#!/usr/bin/env bash
set -euo pipefail

version="0.1.4"
repo="foggy-projects/foggy-runtime-cli"
python_exe="python"
download_dir=""
wheel_path=""
checksums_path=""
user_install=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      version="$2"
      shift 2
      ;;
    --repo)
      repo="$2"
      shift 2
      ;;
    --python)
      python_exe="$2"
      shift 2
      ;;
    --download-dir)
      download_dir="$2"
      shift 2
      ;;
    --wheel)
      wheel_path="$2"
      shift 2
      ;;
    --checksums)
      checksums_path="$2"
      shift 2
      ;;
    --user)
      user_install=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$download_dir" ]]; then
  download_dir="${TMPDIR:-/tmp}/foggy-runtime-cli-install/$version"
fi
mkdir -p "$download_dir"

asset_name="foggy_runtime_cli-${version}-py3-none-any.whl"
release_base="https://github.com/${repo}/releases/download/v${version}"

download_file() {
  local url="$1"
  local out="$2"
  echo "Downloading $url"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$out"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$out"
  else
    echo "curl or wget is required to download release assets." >&2
    exit 1
  fi
}

if [[ -z "$wheel_path" ]]; then
  wheel_path="$download_dir/$asset_name"
  download_file "$release_base/$asset_name" "$wheel_path"
else
  wheel_path="$(cd "$(dirname "$wheel_path")" && pwd)/$(basename "$wheel_path")"
  asset_name="$(basename "$wheel_path")"
fi

if [[ -z "$checksums_path" ]]; then
  checksums_path="$download_dir/SHA256SUMS"
  download_file "$release_base/SHA256SUMS" "$checksums_path"
else
  checksums_path="$(cd "$(dirname "$checksums_path")" && pwd)/$(basename "$checksums_path")"
fi

expected_hash="$(awk -v file="$asset_name" '$2 == file { print tolower($1); exit }' "$checksums_path")"
if [[ -z "$expected_hash" ]]; then
  echo "Could not find $asset_name in $checksums_path" >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  actual_hash="$(sha256sum "$wheel_path" | awk '{ print tolower($1) }')"
else
  actual_hash="$(shasum -a 256 "$wheel_path" | awk '{ print tolower($1) }')"
fi

if [[ "$actual_hash" != "$expected_hash" ]]; then
  echo "SHA256 mismatch for $asset_name. expected=$expected_hash actual=$actual_hash" >&2
  exit 1
fi

echo "SHA256 verified: $asset_name"

pip_args=(-m pip install --upgrade "$wheel_path")
if [[ "$user_install" == "1" ]]; then
  pip_args+=(--user)
fi
"$python_exe" "${pip_args[@]}"

"$python_exe" -m foggy_runtime_cli.main --help | head -n 12
echo "foggy-runtime-cli $version installed."
