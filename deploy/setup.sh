#!/usr/bin/env bash
# ============================================================================
# VPS Setup Script for Solana Sniper + CoinPump Scout
# Tested on: Ubuntu 22.04 / 24.04
# Run as root: bash setup.sh
# ============================================================================
set -euo pipefail

SNIPER_REPO="https://github.com/Trivenidigital/solana-sniper.git"
SCOUT_REPO="https://github.com/Trivenidigital/coinpump-scout.git"
SNIPER_DIR="/opt/sniper"
SCOUT_DIR="/opt/scout"
SERVICE_USER="sniper"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Pre-flight checks ──────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)"
fi

info "Starting VPS setup for Solana Sniper + CoinPump Scout"

# ── 1. System dependencies ─────────────────────────────────────────────────
info "Updating packages and installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git curl wget unzip \
    nginx certbot python3-certbot-nginx \
    sqlite3 jq \
    build-essential

# ── 2. Install uv ──────────────────────────────────────────────────────────
info "Installing uv package manager..."
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# ── 3. Create system user ──────────────────────────────────────────────────
info "Creating system user: ${SERVICE_USER}"
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --create-home --shell /bin/bash "${SERVICE_USER}"
fi

# Install uv for the sniper user too
info "Installing uv for ${SERVICE_USER} user..."
su - "${SERVICE_USER}" -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' || true

# ── 4. Clone repositories ──────────────────────────────────────────────────
info "Cloning repositories..."

if [[ -d "${SNIPER_DIR}" ]]; then
    warn "${SNIPER_DIR} already exists — pulling latest"
    cd "${SNIPER_DIR}" && git pull --ff-only || true
else
    git clone "${SNIPER_REPO}" "${SNIPER_DIR}"
fi

if [[ -d "${SCOUT_DIR}" ]]; then
    warn "${SCOUT_DIR} already exists — pulling latest"
    cd "${SCOUT_DIR}" && git pull --ff-only || true
else
    git clone "${SCOUT_REPO}" "${SCOUT_DIR}"
fi

# Set ownership
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${SNIPER_DIR}" "${SCOUT_DIR}"

# ── 5. Install Python dependencies ─────────────────────────────────────────
info "Installing Python dependencies with uv..."
su - "${SERVICE_USER}" -c "cd ${SCOUT_DIR} && /home/${SERVICE_USER}/.local/bin/uv sync"
su - "${SERVICE_USER}" -c "cd ${SNIPER_DIR} && /home/${SERVICE_USER}/.local/bin/uv sync"

# ── 6. Configure scout.db path ─────────────────────────────────────────────
# The sniper needs to read the scout's database.
# Create a symlink so the default SCOUT_DB_PATH resolves correctly.
info "Linking scout.db for sniper access..."
ln -sf "${SCOUT_DIR}/scout.db" "${SNIPER_DIR}/scout.db" 2>/dev/null || true

# ── 7. Copy systemd service files ──────────────────────────────────────────
info "Installing systemd service files..."
cp "${SNIPER_DIR}/deploy/coinpump-scout.service" /etc/systemd/system/
cp "${SNIPER_DIR}/deploy/solana-sniper.service"  /etc/systemd/system/
cp "${SNIPER_DIR}/deploy/sniper-dashboard.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable coinpump-scout solana-sniper sniper-dashboard

# ── 8. Configure nginx ─────────────────────────────────────────────────────
info "Configuring nginx..."
cp "${SNIPER_DIR}/deploy/nginx.conf" /etc/nginx/sites-available/sniper-dashboard

# Remove default site, enable ours
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/sniper-dashboard /etc/nginx/sites-enabled/sniper-dashboard

nginx -t && systemctl reload nginx

# ── 9. Optional: HTTPS with Let's Encrypt ──────────────────────────────────
echo ""
read -rp "Enter your domain name for HTTPS (or press Enter to skip): " DOMAIN

if [[ -n "${DOMAIN}" ]]; then
    info "Setting up HTTPS for ${DOMAIN}..."
    # Update nginx server_name
    sed -i "s/server_name _;/server_name ${DOMAIN};/" /etc/nginx/sites-available/sniper-dashboard
    nginx -t && systemctl reload nginx
    certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos --register-unsafely-without-email || {
        warn "Certbot failed. You can run it manually later:"
        warn "  certbot --nginx -d ${DOMAIN}"
    }
else
    info "Skipping HTTPS setup (no domain provided)"
fi

# ── 10. Create .env templates ──────────────────────────────────────────────
info "Creating .env templates..."

if [[ ! -f "${SCOUT_DIR}/.env" ]]; then
    cat > "${SCOUT_DIR}/.env" <<'ENVEOF'
# CoinPump Scout Configuration
# Fill in your API keys below

ANTHROPIC_API_KEY=sk-ant-CHANGEME

# Telegram alerts (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Discord alerts (optional)
DISCORD_WEBHOOK_URL=
ENVEOF
    chown "${SERVICE_USER}:${SERVICE_USER}" "${SCOUT_DIR}/.env"
    chmod 600 "${SCOUT_DIR}/.env"
fi

if [[ ! -f "${SNIPER_DIR}/.env" ]]; then
    cat > "${SNIPER_DIR}/.env" <<'ENVEOF'
# Solana Sniper Configuration
# Fill in your values below

# Solana RPC (use a paid RPC for production)
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
SOLANA_WS_URL=wss://api.mainnet-beta.solana.com

# Wallet keypair path (relative to /opt/sniper)
KEYPAIR_PATH=wallet.json

# Scout DB path (symlinked by setup script)
SCOUT_DB_PATH=scout.db

# Risk controls
MAX_BUY_SOL=0.1
MAX_PORTFOLIO_SOL=1.0
MAX_OPEN_POSITIONS=5
STOP_LOSS_PCT=35.0
TAKE_PROFIT_PCT=100.0

# Paper mode (set to false for live trading)
PAPER_MODE=true

# Telegram notifications (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ENVEOF
    chown "${SERVICE_USER}:${SERVICE_USER}" "${SNIPER_DIR}/.env"
    chmod 600 "${SNIPER_DIR}/.env"
fi

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "============================================================================"
info "Setup complete!"
echo "============================================================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit the .env files with your API keys and wallet:"
echo "     sudo -u ${SERVICE_USER} nano ${SCOUT_DIR}/.env"
echo "     sudo -u ${SERVICE_USER} nano ${SNIPER_DIR}/.env"
echo ""
echo "  2. Place your wallet keypair at:"
echo "     ${SNIPER_DIR}/wallet.json"
echo "     (or update KEYPAIR_PATH in .env)"
echo ""
echo "  3. Start the services:"
echo "     systemctl start coinpump-scout"
echo "     systemctl start solana-sniper"
echo "     systemctl start sniper-dashboard"
echo ""
echo "  4. Check status:"
echo "     systemctl status coinpump-scout solana-sniper sniper-dashboard"
echo ""
echo "  5. View logs:"
echo "     journalctl -u coinpump-scout -f"
echo "     journalctl -u solana-sniper -f"
echo "     journalctl -u sniper-dashboard -f"
echo ""
echo "  6. Dashboard available at:"
if [[ -n "${DOMAIN:-}" ]]; then
    echo "     https://${DOMAIN}"
else
    echo "     http://<your-server-ip>"
fi
echo ""
echo "============================================================================"
