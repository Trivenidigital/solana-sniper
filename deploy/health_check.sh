#!/bin/bash
# Hourly health check — sends Telegram alert ONLY when something is wrong
# Install: crontab -e → 7 * * * * /opt/sniper/deploy/health_check.sh

set -euo pipefail

BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN /opt/sniper/.env | cut -d= -f2)
CHAT_ID=$(grep TELEGRAM_CHAT_ID /opt/sniper/.env | cut -d= -f2)
LOG="/opt/sniper/logs/health_check.log"
mkdir -p /opt/sniper/logs

ISSUES=""

# 1. Services running
for svc in solana-sniper coinpump-scout sniper-dashboard; do
  if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
    ISSUES="${ISSUES}❌ ${svc} is DOWN\n"
  fi
done

# 2. Scout cycling (should complete a cycle every ~90s)
LAST_CYCLE=$(journalctl -u coinpump-scout --since "5 minutes ago" --no-pager --output=cat 2>/dev/null | grep -c "Cycle complete" || true)
if [ "$LAST_CYCLE" -eq 0 ]; then
  ISSUES="${ISSUES}❌ Scout: no cycles in 5 min\n"
fi

# 3. Error counts in last hour
SCOUT_ERRORS=$(journalctl -u coinpump-scout --since "1 hour ago" --no-pager --output=cat 2>/dev/null | grep -c '"level":"error"' || true)
SNIPER_ERRORS=$(journalctl -u solana-sniper --since "1 hour ago" --no-pager --output=cat 2>/dev/null | grep -c '"level":"error"' || true)
if [ "$SCOUT_ERRORS" -gt 5 ]; then
  ISSUES="${ISSUES}⚠️ Scout: ${SCOUT_ERRORS} errors in last hour\n"
fi
if [ "$SNIPER_ERRORS" -gt 5 ]; then
  ISSUES="${ISSUES}⚠️ Sniper: ${SNIPER_ERRORS} errors in last hour\n"
fi

# 4. API health
HELIUS_429=$(journalctl -u coinpump-scout --since "1 hour ago" --no-pager --output=cat 2>/dev/null | grep -c "Helius rate limited" || true)
ANTHROPIC_ERR=$(journalctl -u coinpump-scout --since "1 hour ago" --no-pager --output=cat 2>/dev/null | grep -c "credit balance is too low" || true)
RUGCHECK_429=$(journalctl -u coinpump-scout --since "1 hour ago" --no-pager --output=cat 2>/dev/null | grep -c "Rugcheck returned non-200" || true)

if [ "$HELIUS_429" -gt 20 ]; then
  ISSUES="${ISSUES}⚠️ Helius: ${HELIUS_429} rate limits/hr\n"
fi
if [ "$ANTHROPIC_ERR" -gt 0 ]; then
  ISSUES="${ISSUES}❌ Anthropic: credits exhausted\n"
fi
if [ "$RUGCHECK_429" -gt 50 ]; then
  ISSUES="${ISSUES}⚠️ Rugcheck: ${RUGCHECK_429} 429s/hr\n"
fi

# 5. DB health (quick check, not full integrity)
if ! sqlite3 /opt/sniper/sniper.db "SELECT 1;" >/dev/null 2>&1; then
  ISSUES="${ISSUES}❌ Sniper DB corrupted\n"
fi
if ! sqlite3 /opt/scout/scout.db "SELECT 1;" >/dev/null 2>&1; then
  ISSUES="${ISSUES}❌ Scout DB corrupted\n"
fi

# 6. Position stats
OPEN_POS=$(sqlite3 /opt/sniper/sniper.db "SELECT COUNT(*) FROM positions WHERE status='open';" 2>/dev/null || echo "?")
EXPOSURE=$(sqlite3 /opt/sniper/sniper.db "SELECT ROUND(COALESCE(SUM(entry_sol),0),2) FROM positions WHERE status='open';" 2>/dev/null || echo "?")
REALIZED=$(sqlite3 /opt/sniper/sniper.db "SELECT ROUND(COALESCE(SUM(pnl_sol),0),4) FROM positions WHERE status='closed';" 2>/dev/null || echo "?")

# 7. Alerts fired in last hour
ALERTS_FIRED=$(journalctl -u coinpump-scout --since "1 hour ago" --no-pager --output=cat 2>/dev/null | grep '"alerts_fired"' | grep -oP '"alerts_fired":\s*\K[0-9]+' | awk '{s+=$1}END{print s+0}' || echo "0")

# Send Telegram always — success or failure
if [ -n "$ISSUES" ]; then
  MSG="🚨 SNIPER HEALTH CHECK
$(date -u +%H:%M)Z

${ISSUES}
📊 Positions: ${OPEN_POS} (${EXPOSURE} SOL)
💰 Realized: ${REALIZED} SOL
📡 Alerts/hr: ${ALERTS_FIRED}"
else
  MSG="✅ SNIPER OK — $(date -u +%H:%M)Z
📊 Positions: ${OPEN_POS} (${EXPOSURE} SOL)
💰 Realized: ${REALIZED} SOL
📡 Alerts/hr: ${ALERTS_FIRED}
🔄 Helius 429s: ${HELIUS_429} | Rugcheck 429s: ${RUGCHECK_429}"
fi
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d chat_id="$CHAT_ID" -d "text=$(echo -e "$MSG")" >/dev/null 2>&1

# Always log
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | pos=${OPEN_POS} exp=${EXPOSURE} pnl=${REALIZED} alerts=${ALERTS_FIRED} scout_err=${SCOUT_ERRORS} sniper_err=${SNIPER_ERRORS} helius=${HELIUS_429} rugcheck=${RUGCHECK_429} anthropic=${ANTHROPIC_ERR} issues=$([ -n "$ISSUES" ] && echo 'YES' || echo 'NONE')" >> "$LOG"
