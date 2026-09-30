#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
args=(--env-file config/.env -f compose.yaml)
if [[ -f compose.override.yaml ]]; then
  args+=(-f compose.override.yaml)
elif [[ -f compose.override.yml ]]; then
  args+=(-f compose.override.yml)
fi
exec docker compose "${args[@]}" -f compose.live.yaml "$@"
