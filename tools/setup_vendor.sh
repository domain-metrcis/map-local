#!/usr/bin/env bash
# One-time setup: download + extract ungoogled-chromium into map-local/vendor/.
# Idempotent — re-running is safe and fast (skips work that's already done).
#
# maps_checker.py auto-discovers vendor/ungoogled-chromium/chrome at runtime, so
# you don't need any --chrome flag after running this. Google Maps is NOT behind
# Cloudflare Turnstile, so (unlike ahref-local) there is no cf-autoclick
# extension / master-profile to set up — just the browser binary.
#
# Usage:
#   bash tools/setup_vendor.sh
#
# To upgrade chromium later:
#   1. Update CHROMIUM_VERSION below
#   2. Delete vendor/ungoogled-chromium/ and vendor/ungoogled-chromium.tar.xz
#   3. Re-run this script

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAP_DIR="$(dirname "${SCRIPT_DIR}")"
VENDOR_DIR="${MAP_DIR}/vendor"

# ─── Pinned version ─────────────────────────────────────────────────────────
CHROMIUM_VERSION="149.0.7827.53-1"
CHROMIUM_TARBALL_URL="https://github.com/ungoogled-software/ungoogled-chromium-portablelinux/releases/download/${CHROMIUM_VERSION}/ungoogled-chromium-${CHROMIUM_VERSION}-x86_64_linux.tar.xz"
CHROMIUM_TARBALL="${VENDOR_DIR}/ungoogled-chromium.tar.xz"
CHROMIUM_DIR="${VENDOR_DIR}/ungoogled-chromium"
CHROMIUM_INNER="${VENDOR_DIR}/ungoogled-chromium-${CHROMIUM_VERSION}-x86_64_linux"

mkdir -p "${VENDOR_DIR}"

if [[ -x "${CHROMIUM_DIR}/chrome" ]]; then
  echo "[*] ungoogled-chromium already present at ${CHROMIUM_DIR}/chrome — skipping"
else
  if [[ ! -f "${CHROMIUM_TARBALL}" ]]; then
    echo "[*] Downloading ungoogled-chromium ${CHROMIUM_VERSION}..."
    curl -fL --progress-bar -o "${CHROMIUM_TARBALL}" "${CHROMIUM_TARBALL_URL}"
  else
    echo "[*] Tarball already cached at ${CHROMIUM_TARBALL}"
  fi

  echo "[*] Extracting tarball to ${VENDOR_DIR}..."
  tar -xJf "${CHROMIUM_TARBALL}" -C "${VENDOR_DIR}"

  if [[ ! -d "${CHROMIUM_INNER}" ]]; then
    echo "❌ Expected extracted dir ${CHROMIUM_INNER} not found." >&2
    echo "   The tarball layout may have changed. Check the contents:" >&2
    echo "     tar -tJf ${CHROMIUM_TARBALL} | head" >&2
    exit 1
  fi

  mv "${CHROMIUM_INNER}" "${CHROMIUM_DIR}"
  chmod +x "${CHROMIUM_DIR}/chrome" "${CHROMIUM_DIR}/chromedriver" 2>/dev/null || true

  echo "[*] Verifying binary..."
  if ! "${CHROMIUM_DIR}/chrome" --version; then
    echo "❌ vendor/ungoogled-chromium/chrome failed to run --version." >&2
    echo "   You may be missing system libraries. On Ubuntu try:" >&2
    echo "     sudo apt install -y libnss3 libatk-bridge2.0-0 libxkbcommon0 \\" >&2
    echo "                         libxcomposite1 libxdamage1 libxrandr2 libgbm1 \\" >&2
    echo "                         libpango-1.0-0 libcairo2 libasound2t64" >&2
    exit 1
  fi
  echo "✅ Chromium ready at ${CHROMIUM_DIR}/chrome"
fi

cat <<EOF

╔══════════════════════════════════════════════════════════════╗
║                  ✅  VENDOR SETUP COMPLETE                   ║
╚══════════════════════════════════════════════════════════════╝

  Chromium: ${CHROMIUM_DIR}/chrome

maps_checker.py auto-discovers it at runtime. Try:

  cd ${MAP_DIR}
  python -m venv .venv && .venv/bin/pip install -r requirements.txt
  .venv/bin/python maps_checker.py --workers 1 --no-proxy --headless

EOF
