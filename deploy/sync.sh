#!/usr/bin/env bash
# Sync code to VPS without overwriting .env or DB files
# Usage: ./deploy/sync.sh [root@ip]

set -euo pipefail

VPS="${1:-root@149.28.125.16}"

echo "Checkpointing WAL files before stop (prevents DB corruption)..."
ssh "$VPS" "
  sqlite3 /opt/scout/scout.db 'PRAGMA wal_checkpoint(TRUNCATE);' 2>/dev/null || true
  sqlite3 /opt/sniper/sniper.db 'PRAGMA wal_checkpoint(TRUNCATE);' 2>/dev/null || true
  echo 'WAL checkpointed'
"

echo "Stopping services for clean deploy..."
ssh "$VPS" "
  systemctl stop coinpump-scout solana-sniper sniper-dashboard 2>/dev/null
  sleep 2
  rm -f /opt/scout/scout.db-wal /opt/scout/scout.db-shm 2>/dev/null
  rm -f /opt/sniper/sniper.db-wal /opt/sniper/sniper.db-shm 2>/dev/null
  echo 'Services stopped, WAL cleaned'
"

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
ssh "$VPS" 'bash -s' << 'MIGRATIONS'
  # Scout DB migrations (safe — ALTER fails silently if column exists)
  sqlite3 /opt/scout/scout.db "ALTER TABLE alerts ADD COLUMN market_cap_usd REAL DEFAULT 0;" 2>/dev/null || true
  for col in smart_money_buys:INTEGER whale_buys:INTEGER liquidity_locked:INTEGER volume_spike:INTEGER volume_spike_ratio:REAL holder_gini_healthy:INTEGER whale_txns_1h:INTEGER social_score:REAL has_twitter:INTEGER has_telegram:INTEGER has_github:INTEGER on_coingecko:INTEGER multi_dex:INTEGER dex_count:INTEGER news_mentions:INTEGER news_sentiment:REAL has_news:INTEGER; do
    name=${col%%:*}; type=${col##*:}
    sqlite3 /opt/scout/scout.db "ALTER TABLE candidates ADD COLUMN $name $type DEFAULT 0;" 2>/dev/null || true
  done
  sqlite3 /opt/scout/scout.db "
    CREATE INDEX IF NOT EXISTS idx_alerts_contract ON alerts (contract_address);
    CREATE TABLE IF NOT EXISTS vol_gate_snapshots (
      contract_address TEXT, vol_5min REAL, recorded_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_vol_gate_snapshots_contract
      ON vol_gate_snapshots (contract_address, recorded_at DESC);
  " 2>/dev/null || true

  # Injections DB
  sqlite3 /opt/scout/injections.db "
    CREATE TABLE IF NOT EXISTS smart_money_injections (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token_mint TEXT NOT NULL,
      wallet_address TEXT NOT NULL,
      tx_signature TEXT,
      source TEXT DEFAULT 'websocket',
      detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      processed INTEGER DEFAULT 0,
      UNIQUE(token_mint, tx_signature)
    );
    CREATE INDEX IF NOT EXISTS idx_smi_unprocessed
      ON smart_money_injections(processed, detected_at);
  " 2>/dev/null || true

  # Sniper DB migrations
  sqlite3 /opt/sniper/sniper.db "ALTER TABLE positions ADD COLUMN partial_exit_tier INTEGER DEFAULT 0;" 2>/dev/null || true
  sqlite3 /opt/sniper/sniper.db "
    CREATE TABLE IF NOT EXISTS kv_store (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
  " 2>/dev/null || true
  echo 'DB migrations done'
MIGRATIONS

echo "Restarting services..."
ssh "$VPS" "
  systemctl restart coinpump-scout solana-sniper sniper-dashboard 2>/dev/null
  sleep 5
  echo 'Scout:' \$(systemctl is-active coinpump-scout)
  echo 'Sniper:' \$(systemctl is-active solana-sniper)
  echo 'Dashboard:' \$(systemctl is-active sniper-dashboard)
"

echo ""
echo "Running post-deploy health check..."
ERRORS=$(ssh "$VPS" "
  sleep 5

  # Check services are still running (not crash-looping)
  SCOUT_STATUS=\$(systemctl is-active coinpump-scout)
  SNIPER_STATUS=\$(systemctl is-active solana-sniper)
  DASH_STATUS=\$(systemctl is-active sniper-dashboard)
  FAILED=0

  if [ \"\$SCOUT_STATUS\" != 'active' ]; then echo 'FAIL: Scout is not running'; FAILED=1; fi
  if [ \"\$SNIPER_STATUS\" != 'active' ]; then echo 'FAIL: Sniper is not running'; FAILED=1; fi
  if [ \"\$DASH_STATUS\" != 'active' ]; then echo 'FAIL: Dashboard is not running'; FAILED=1; fi

  # Check for errors in last 30 seconds of logs
  SCOUT_ERRORS=\$(journalctl -u coinpump-scout --since '30 seconds ago' --no-pager --output=cat 2>/dev/null | grep -c '\"level\":\"error\"' || true)
  SNIPER_ERRORS=\$(journalctl -u solana-sniper --since '30 seconds ago' --no-pager --output=cat 2>/dev/null | grep -c '\"level\":\"error\"' || true)

  if [ \"\$SCOUT_ERRORS\" -gt 0 ]; then
    echo \"FAIL: Scout has \$SCOUT_ERRORS errors in logs:\"
    journalctl -u coinpump-scout --since '30 seconds ago' --no-pager --output=cat 2>/dev/null | grep '\"level\":\"error\"' | tail -3
    FAILED=1
  fi
  if [ \"\$SNIPER_ERRORS\" -gt 0 ]; then
    echo \"FAIL: Sniper has \$SNIPER_ERRORS errors in logs:\"
    journalctl -u solana-sniper --since '30 seconds ago' --no-pager --output=cat 2>/dev/null | grep '\"level\":\"error\"' | tail -3
    FAILED=1
  fi

  # Check DB accessibility (quick SELECT, not full integrity — services are writing WAL)
  SCOUT_DB_OK=\$(sqlite3 /opt/scout/scout.db 'SELECT 1;' 2>/dev/null || echo 'FAIL')
  SNIPER_DB_OK=\$(sqlite3 /opt/sniper/sniper.db 'SELECT 1;' 2>/dev/null || echo 'FAIL')
  INJ_DB_OK=\$(sqlite3 /opt/scout/injections.db 'SELECT 1;' 2>/dev/null || echo 'FAIL')
  if [ \"\$SCOUT_DB_OK\" = 'FAIL' ]; then echo 'FAIL: Scout DB inaccessible'; FAILED=1; fi
  if [ \"\$SNIPER_DB_OK\" = 'FAIL' ]; then echo 'FAIL: Sniper DB inaccessible'; FAILED=1; fi
  if [ \"\$INJ_DB_OK\" = 'FAIL' ]; then echo 'FAIL: Injections DB inaccessible'; FAILED=1; fi

  # Check dashboard responds
  DASH_OK=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080 2>/dev/null || echo '000')
  if [ \"\$DASH_OK\" != '200' ]; then echo \"FAIL: Dashboard HTTP \$DASH_OK\"; FAILED=1; fi

  if [ \"\$FAILED\" -eq 0 ]; then
    echo 'ALL CHECKS PASSED'
  fi
")

echo "$ERRORS"

if echo "$ERRORS" | grep -q "FAIL:"; then
  echo ""
  echo "!!! DEPLOY HAS ISSUES — CHECK ABOVE !!!"
  exit 1
fi

echo ""
echo "Deploy complete."
