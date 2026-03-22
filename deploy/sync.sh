#!/usr/bin/env bash
# Sync code to VPS without overwriting .env or DB files
# Usage: ./deploy/sync.sh [root@ip]

set -euo pipefail

VPS="${1:-root@149.28.125.16}"

echo "Syncing scout code..."
rsync -az --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
  --exclude='*.pyc' --exclude='*.db' --exclude='.env' --exclude='*.xlsx' \
  --exclude='*.png' --exclude='.DS_Store' --exclude='wallet.json' \
  ~/coinpump-scout/ "$VPS:/opt/scout/"

echo "Syncing sniper code..."
rsync -az --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
  --exclude='*.pyc' --exclude='*.db' --exclude='.env' --exclude='*.xlsx' \
  --exclude='.DS_Store' --exclude='wallet.json' \
  ~/solana-sniper/ "$VPS:/opt/sniper/"

echo "Running DB migrations..."
ssh "$VPS" "
  # Scout DB migrations (safe — IF NOT EXISTS / try-catch)
  sqlite3 /opt/scout/scout.db '
    ALTER TABLE alerts ADD COLUMN market_cap_usd REAL DEFAULT 0;
  ' 2>/dev/null || true
  sqlite3 /opt/scout/scout.db '
    CREATE INDEX IF NOT EXISTS idx_alerts_contract ON alerts (contract_address);
    CREATE TABLE IF NOT EXISTS vol_gate_snapshots (
      contract_address TEXT, vol_5min REAL, recorded_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_vol_gate_snapshots_contract
      ON vol_gate_snapshots (contract_address, recorded_at DESC);
  ' 2>/dev/null || true

  # Sniper DB migrations
  sqlite3 /opt/sniper/sniper.db '
    ALTER TABLE positions ADD COLUMN partial_exit_tier INTEGER DEFAULT 0;
  ' 2>/dev/null || true
  echo 'DB migrations done'
"

echo "Restarting services..."
ssh "$VPS" "
  systemctl restart coinpump-scout solana-sniper sniper-dashboard 2>/dev/null
  sleep 3
  echo 'Scout:' \$(systemctl is-active coinpump-scout)
  echo 'Sniper:' \$(systemctl is-active solana-sniper)
  echo 'Dashboard:' \$(systemctl is-active sniper-dashboard)
"

echo "Deploy complete."
