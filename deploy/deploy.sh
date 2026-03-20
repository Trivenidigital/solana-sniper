#!/usr/bin/env bash
# ============================================================================
# Quick Deploy Script
# Copies setup files to a VPS via SSH and runs the setup script.
#
# Usage: ./deploy/deploy.sh user@ip
#
# Example:
#   ./deploy/deploy.sh root@203.0.113.10
#   ./deploy/deploy.sh ubuntu@my-vps.example.com
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Argument check ──────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 user@host"
    echo ""
    echo "Examples:"
    echo "  $0 root@203.0.113.10"
    echo "  $0 ubuntu@my-vps.example.com"
    exit 1
fi

TARGET="$1"

# ── 1. Test SSH connection ──────────────────────────────────────────────────
info "Testing SSH connection to ${TARGET}..."
ssh -o ConnectTimeout=10 "${TARGET}" "echo 'SSH connection OK'" || \
    error "Cannot connect to ${TARGET}. Check your SSH key and IP."

# ── 2. Create remote staging directory ──────────────────────────────────────
info "Creating staging directory on remote host..."
ssh "${TARGET}" "mkdir -p /tmp/sniper-deploy"

# ── 3. Copy deploy files ───────────────────────────────────────────────────
info "Copying deployment files..."
scp -r "${SCRIPT_DIR}/setup.sh" \
       "${SCRIPT_DIR}/solana-sniper.service" \
       "${SCRIPT_DIR}/coinpump-scout.service" \
       "${SCRIPT_DIR}/sniper-dashboard.service" \
       "${SCRIPT_DIR}/nginx.conf" \
       "${TARGET}:/tmp/sniper-deploy/"

# ── 4. Copy .env files if they exist locally ────────────────────────────────
if [[ -f "${PROJECT_DIR}/.env" ]]; then
    info "Copying solana-sniper .env..."
    scp "${PROJECT_DIR}/.env" "${TARGET}:/tmp/sniper-deploy/sniper.env"
else
    warn "No .env found at ${PROJECT_DIR}/.env — you will need to create it on the VPS"
fi

SCOUT_DIR="$(dirname "${PROJECT_DIR}")/coinpump-scout"
if [[ -f "${SCOUT_DIR}/.env" ]]; then
    info "Copying coinpump-scout .env..."
    scp "${SCOUT_DIR}/.env" "${TARGET}:/tmp/sniper-deploy/scout.env"
else
    warn "No .env found at ${SCOUT_DIR}/.env — you will need to create it on the VPS"
fi

# ── 5. Copy wallet if it exists ─────────────────────────────────────────────
if [[ -f "${PROJECT_DIR}/wallet.json" ]]; then
    info "Copying wallet.json..."
    scp "${PROJECT_DIR}/wallet.json" "${TARGET}:/tmp/sniper-deploy/wallet.json"
else
    warn "No wallet.json found — you will need to create one on the VPS"
fi

# ── 6. Run setup script ────────────────────────────────────────────────────
info "Running setup script on remote host..."
ssh -t "${TARGET}" "sudo bash /tmp/sniper-deploy/setup.sh"

# ── 7. Deploy .env files to final locations ─────────────────────────────────
info "Deploying configuration files to final locations..."
ssh "${TARGET}" bash -s <<'REMOTE_SCRIPT'
set -euo pipefail

if [[ -f /tmp/sniper-deploy/sniper.env ]]; then
    sudo cp /tmp/sniper-deploy/sniper.env /opt/sniper/.env
    sudo chown sniper:sniper /opt/sniper/.env
    sudo chmod 600 /opt/sniper/.env
    echo "[INFO] Deployed sniper .env"
fi

if [[ -f /tmp/sniper-deploy/scout.env ]]; then
    sudo cp /tmp/sniper-deploy/scout.env /opt/scout/.env
    sudo chown sniper:sniper /opt/scout/.env
    sudo chmod 600 /opt/scout/.env
    echo "[INFO] Deployed scout .env"
fi

if [[ -f /tmp/sniper-deploy/wallet.json ]]; then
    sudo cp /tmp/sniper-deploy/wallet.json /opt/sniper/wallet.json
    sudo chown sniper:sniper /opt/sniper/wallet.json
    sudo chmod 600 /opt/sniper/wallet.json
    echo "[INFO] Deployed wallet.json"
fi

# Clean up staging directory
rm -rf /tmp/sniper-deploy
REMOTE_SCRIPT

# ── 8. Start services ──────────────────────────────────────────────────────
info "Starting services..."
ssh "${TARGET}" "sudo systemctl start coinpump-scout solana-sniper sniper-dashboard"

# ── 9. Verify ───────────────────────────────────────────────────────────────
info "Checking service status..."
ssh "${TARGET}" "sudo systemctl status coinpump-scout solana-sniper sniper-dashboard --no-pager" || true

echo ""
echo "============================================================================"
info "Deployment complete!"
echo "============================================================================"
echo ""
echo "  Dashboard:  http://${TARGET##*@}"
echo "  Logs:       ssh ${TARGET} 'journalctl -u solana-sniper -f'"
echo ""
