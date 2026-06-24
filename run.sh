#!/usr/bin/env bash
# Interactive launcher for maps_checker.py
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV_PY=""
if [[ -x ".venv/bin/python3" ]]; then
    VENV_PY=".venv/bin/python3"
elif [[ -x "venv/bin/python3" ]]; then
    VENV_PY="venv/bin/python3"
else
    VENV_PY="$(command -v python3)"
fi

echo "╔════════════════════════════════════════╗"
echo "║     Google Maps Local Checker          ║"
echo "╚════════════════════════════════════════╝"
echo ""

read -rp "Number of workers (default: 3): " workers
workers="${workers:-3}"

echo ""
echo "[*] Starting maps_checker.py --workers ${workers} --no-proxy"
echo ""

exec "$VENV_PY" maps_checker.py --workers "${workers}" --no-proxy "$@"
