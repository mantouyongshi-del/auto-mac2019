#!/bin/bash
# 模型健康与跑批进度巡检脚本（由 launchd 每10分钟调用）
# 正常时静默写日志；异常时本地语音+通知报警
LOG_DIR="/Users/alili/laya/server_logs"
CHECK_LOG="$LOG_DIR/healthcheck.log"
BATCH_LOG="/tmp/batch_run.log"
OUT_DIR="/Users/alili/laya/民生行业跑批结果"
VENV_PY="/Users/alili/laya/.venv39/bin/python3"

MODELS=(deepseek:8000 qianwen:8001 doubao:8002 wenxin:8003 yuanbao:8004)
problems=""

ts() { date "+%Y-%m-%d %H:%M:%S"; }

# --- 1. 模型服务健康 ---
for m in "${MODELS[@]}"; do
  name="${m%%:*}"; port="${m##*:}"
  code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port/" 2>/dev/null)
  if [ "$code" != "200" ]; then
    problems="${problems}【${name}服务异常 HTTP=${code}】"
  fi
done

# --- 2. 跑批进程 ---
if ! pgrep -f minsheng_batch_runner.py >/dev/null 2>&1; then
  problems="${problems}【跑批进程未运行】"
fi

# --- 3. 跑批进度（最近进度行 + 落盘统计） ---
progress_line=$(tail -3 "$BATCH_LOG" 2>/dev/null | grep -E "行业|/69|/6[0-9]|完成|休息" | tail -1)
done_files=$(find "$OUT_DIR" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
done_industries=$(find "$OUT_DIR" -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
ind_count=$((done_industries - 1)); [ $ind_count -lt 0 ] && ind_count=0

# --- 4. 报警 ---
if [ -n "$problems" ]; then
  msg="$(ts) [异常] $problems | 进度: 已跑${ind_count}行业 ${done_files}个结果 | $progress_line"
  echo "$msg" >> "$CHECK_LOG"
  # 语音报警
  /usr/bin/say -v Tingting "警告：模型巡检发现异常，请检查" 2>/dev/null &
  # 桌面通知
  osascript -e "display notification \"$problems\" with title \"模型巡检报警\" sound name \"Glass\"" 2>/dev/null &
  exit 0
fi

# --- 正常：静默记录 ---
echo "$(ts) [正常] 5模型在线 | 跑批运行中 | 已跑${ind_count}行业 ${done_files}个结果 | $progress_line" >> "$CHECK_LOG"
exit 0
