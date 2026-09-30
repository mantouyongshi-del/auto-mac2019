#!/bin/bash
# 跑批守护：检测 minsheng_batch_runner.py 是否存活，挂了自动拉起并报警
BATCH_DIR="/Users/alili/laya"
LOG="/tmp/batch_watchdog.log"
HEALTH_LOG="/Users/alili/laya/server_logs/batch_watchdog.log"

if ! pgrep -f "minsheng_batch_runner.py" > /dev/null; then
    TS=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$TS] ⚠️ 检测到跑批进程消失，自动重启" >> "$LOG"
    cd "$BATCH_DIR"
    nohup .venv39/bin/python3 minsheng_batch_runner.py >> /tmp/batch_run.log 2>&1 &
    echo "[$TS] ✅ 已重新拉起 PID $!" >> "$LOG"
    # 语音+通知报警
    say "警告，模型跑批进程异常退出，已自动重启" &
    osascript -e 'display notification "跑批进程消失，已自动重启" with title "跑批守护" sound name "Glass"' 2>/dev/null &
else
    TS=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$TS] 跑批正常（$（pgrep -f minsheng_batch_runner.py | head -1））" >> "$HEALTH_LOG"
fi

# fix_runner（缺口补齐进程）守护——防竞态双实例
FIX_N=$(pgrep -f "fix_runner.py" | wc -l | tr -d ' ')
if [ "$FIX_N" -eq 0 ]; then
    TS=$(date "+%Y-%m-%d %H:%M:%S")
    rm -f "$BATCH_DIR/fix_runner.lock"   # 进程已死，清理残留锁
    cd "$BATCH_DIR"
    nohup .venv39/bin/python3 fix_runner.py >> /tmp/fix_run.log 2>&1 &
    echo "[$TS] ✅ 补齐进程已拉起 PID $!" >> "$LOG"
elif [ "$FIX_N" -gt 1 ]; then
    TS=$(date "+%Y-%m-%d %H:%M:%S")
    FIRST=$(pgrep -f "fix_runner.py" | sort -n | head -1)
    for p in $(pgrep -f "fix_runner.py" | sort -n | tail -n +2); do
        kill -9 "$p" 2>/dev/null
    done
    echo "[$TS] ⚠️ 检测到多个 fix_runner，保留 PID $FIRST 并清理其余" >> "$LOG"
fi
