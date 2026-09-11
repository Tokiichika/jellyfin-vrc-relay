#!/usr/bin/env bash
# Run from a downloaded release: sudo bash install.sh
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3：请先运行 sudo apt install python3" >&2
  exit 1
fi
if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo bash install.sh，以便设置容器配置目录的权限。" >&2
  exit 1
fi
exec python3 deploy.py "$@"
