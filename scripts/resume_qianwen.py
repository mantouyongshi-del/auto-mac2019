#!/usr/bin/env python3
"""定时恢复千问：清空 paused_models.json 中的 qianwen，恢复跑批调用。
由 launchd 一次性任务（com.laya.resume-qianwen）在冷却期结束后触发。
"""
import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
PAUSE_FILE = ROOT / "paused_models.json"
LOG_FILE = Path("/tmp/resume_qianwen.log")


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    if not PAUSE_FILE.exists():
        log("paused_models.json 不存在，无需恢复")
        return
    data = json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
    if "qianwen" not in data:
        log("qianwen 不在暂停列表（可能已手动恢复），跳过")
        return
    data.remove("qianwen")
    PAUSE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ 千问已自动恢复，当前暂停列表: {data}")
    log("提示：若千问仍弹人工验证，系统会自动再次暂停并报警，无需人工干预。")


if __name__ == "__main__":
    main()
