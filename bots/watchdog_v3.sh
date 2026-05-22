#!/bin/bash
# 🛡️ trader.py v27.5 守护脚本 — 三重检测永不放弃
set -euo pipefail
BOT_DIR="/home/openclaw/bots"
LOG="$BOT_DIR/logs/watchdog.log"
PID_FILE="$BOT_DIR/watchdog_v3.pid"
TRADER_PY="$BOT_DIR/trader.py"

echo "$$" > "$PID_FILE"
log() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

cleanup_bot() {
    local pids=$(pgrep -f "python3.*trader.py" 2>/dev/null || true)
    [ -n "$pids" ] && for pid in $pids; do kill "$pid" 2>/dev/null || true; done
    sleep 2
    pids=$(pgrep -f "python3.*trader.py" 2>/dev/null || true)
    [ -n "$pids" ] && for pid in $pids; do kill -9 "$pid" 2>/dev/null || true; done
    rm -f "$BOT_DIR/trader.lock"
}

start_bot() {
    cleanup_bot
    cd "$BOT_DIR"
    nohup python3 "$TRADER_PY" >> "$BOT_DIR/logs/trader_stdout.log" 2>&1 &
    local pid=$!
    log "✅ Bot 已启动 (PID $pid)"
    echo "$pid"
}

# 三重检测
bot_healthy() {
    local pid="${1:-}"
    # 1. 进程检测
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    # 2. 日志活性检测（最近120秒内有新日志就不是卡死）
    local last_log=$(stat -c %Y "$BOT_DIR/logs/trader_log.txt" 2>/dev/null || echo 0)
    now=$(date +%s)
    [ $((now - last_log)) -lt 120 ] || return 1
    # 3. prices.json 300秒未更新=卡死
    if [ -f "$BOT_DIR/prices.json" ]; then
        local prices_ts=$(python3 -c "import json; print(int(json.load(open('$BOT_DIR/prices.json')).get('ts',0)))" 2>/dev/null || echo 0)
        [ $((now - prices_ts)) -lt 300 ] || return 1
    fi
    return 0
}

cleanup_bot
log "━━━━━━━━━━━━━━━━━━━━━━"
log "🛡️ watchdog_v3.1 启动(三重检测)"
log "━━━━━━━━━━━━━━━━━━━━━━"

BOT_PID=$(start_bot)
BOT_START_TIME=$(date +%s)
RESTART_COUNT=0
WINDOW_START=$(date +%s)

while true; do
    sleep 5
    
    if bot_healthy "$BOT_PID"; then
        RESTART_COUNT=0
        continue
    fi
    
    # Bot不健康
    now=$(date +%s)
    last_log_age=9999
    [ -f "$BOT_DIR/logs/trader_log.txt" ] && last_log_age=$((now - $(stat -c %Y "$BOT_DIR/logs/trader_log.txt")))

    # 启动后60秒缓冲期，不判定为卡死
    if [ $((now - BOT_START_TIME)) -lt 60 ]; then
        if ! kill -0 "$BOT_PID" 2>/dev/null; then
            log "❌ Bot启动后立即死亡 → 重启"
        else
            continue
        fi
    elif ! kill -0 "$BOT_PID" 2>/dev/null; then
        log "❌ Bot进程已死(最后日志${last_log_age}s前) → 重启"
    else
        log "🔴 Bot卡死(进程存活但日志${last_log_age}s未更新) → 强杀重启"
        kill -9 "$BOT_PID" 2>/dev/null || true
    fi
    
    # 指数退避
    if [ $((now - WINDOW_START)) -gt 600 ]; then
        RESTART_COUNT=0
        WINDOW_START=$now
    fi
    RESTART_COUNT=$((RESTART_COUNT + 1))
    if [ "$RESTART_COUNT" -gt 10 ]; then
        wait_sec=$((RESTART_COUNT * 10))
        [ "$wait_sec" -gt 600 ] && wait_sec=600
        log "⏳ 连续重启${RESTART_COUNT}次, 退避${wait_sec}s"
        sleep "$wait_sec"
    fi
    
    cleanup_bot
    BOT_PID=$(start_bot)
    BOT_START_TIME=$(date +%s)
done
