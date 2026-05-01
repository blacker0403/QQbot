#!/usr/bin/env bash
set -euo pipefail

runtime_dir="${1:-/opt/Linux_bot}"
checkout_dir="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$runtime_dir"

tar -C "$checkout_dir" \
  --exclude='./.git' \
  --exclude='./.env' \
  --exclude='./.venv' \
  --exclude='./config.yaml' \
  --exclude='./data' \
  --exclude='./logs' \
  --exclude='./__pycache__' \
  --exclude='./.pytest_cache' \
  -cf - . | tar -C "$runtime_dir" -xf -

if [ ! -f "$runtime_dir/config.yaml" ]; then
  cp "$checkout_dir/config.example.yaml" "$runtime_dir/config.yaml"
  echo "Created $runtime_dir/config.yaml from config.example.yaml; edit it before starting the bot."
fi

mkdir -p "$runtime_dir/data/avatar_pool" "$runtime_dir/logs"
