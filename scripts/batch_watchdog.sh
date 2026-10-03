#!/bin/bash
# 跑批守护：检测 minsheng_batch_runner.py 是否存活，挂了自动拉起并报警
BATCH_DIR="/Users/alili/laya"
LOG="/tmp/batch_watchdog.log"
HEALTH_LOG="/Users/alili/laya/server_logs/batch_watchdog.log"

if ! pgrep -f "minsheng_batch_runner.py" > /dev/null; then
    TS=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$TS] ⚠️ 检测到跑批进程消失，自动重启" >> "$LOG"
    cd "$BATCH_DIR"
    # 子 shell 包裹 + 绝对路径：避免 launchd 会话结束清理后台进程
    (nohup /Users/alili/laya/.venv39/bin/python3 /Users/alili/laya/minsheng_batch_runner.py >> /tmp/batch_run.log 2>&1 &)
    echo "[$TS] ✅ 已重新拉起 PID $!" >> "$LOG"
    # 语音+通知报警
    say "警告，模型跑批进程异常退出，已自动重启" &
    osascript -e 'display notification "跑批进程消失，已自动重启" with title "跑批守护" sound name "Glass"' 2>/dev/null &
else
    TS=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$TS] 跑批正常（$（pgrep -f minsheng_batch_runner.py | head -1））" >> "$HEALTH_LOG"
fi

# fix_runner（缺口补齐进程）守护：已移交 com.laya.fixrunner launchd 服务（KeepAlive 原生自愈），此处不再重复拉起，避免双管理竞争。
# fix_runner（缺口补齐进程）守护——以锁文件 PID 判断存活
# 注意：不能用 pgrep -f 匹配，否则会误把命令行含 fix_runner.py 字样的其他进程
# （如操作命令）计入，导致误杀真正的 fix_runner。锁文件 PID + kill -0 最可靠。
FIX_PID=""
if [ -f "$BATCH_DIR/fix_runner.lock" ]; then
    FIX_PID=$(cat "$BATCH_DIR/fix_runner.lock" 2>/dev/null | tr -d '[:space:]')
fi
# fix_runner 自愈已由 launchd com.laya.fixrunner 承担（KeepAlive=true），watchdog 不再拉起。
# 保留判断仅为记录：若 launchd 未接管（旧部署），此处给出提示。
if [ -z "$FIX_PID" ] || ! kill -0 "$FIX_PID" 2>/dev/null; then
    TS=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$TS] ⚠️ 检测到 fix_runner 缺失（应由 com.laya.fixrunner 拉起），等待 launchd KeepAlive" >> "$LOG"
fi
