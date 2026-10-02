#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型巡检（每 30 分钟由 launchd 触发）：
1. 检查 5 个模型服务健康（login/captcha/busy/last_success）
2. 暂停管理：检测到验证但未暂停 → 自动暂停+报警；验证解除但暂停中 → 自动恢复
3. 服务挂 → 重启对应 launchd 服务 + 报警
4. 跑批停滞（主 runner 长时间无新题）→ 报警并重启 runner
5. 全部结果写入 /tmp/model_watch.log
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# busy 卡死检测：连续 busy 超过该秒数视为卡死（正常回答最长约260s，6分钟阈值留裕量）
BUSY_STUCK_SEC = 360
# 静默失效检测：last_success_at 距今超过该小时数且模型看似正常 → 自动重启一次
SILENT_HOURS = 6
# 静默重启防抖：同一模型距上次静默重启至少间隔秒数才再次触发
SILENT_COOLDOWN = 3600
# 状态持久化文件：launchd 每次触发是独立进程，内存变量跨进程不保留，
# 必须落盘才能让 busy 卡死 / 静默失效检测跨巡检生效。
STATE_FILE = "/tmp/model_watch_state.json"

ROOT = Path(__file__).parent.parent  # /Users/alili/laya
API_KEY = os.environ.get("LAYA_API_KEY", "laya-local-model-key")
LOG = "/tmp/model_watch.log"

MODEL_URLS = {
    "deepseek": 8000,
    "qianwen": 8001,
    "doubao": 8002,
    "wenxin": 8003,
    "yuanbao": 8004,
}

PAUSED_FILE = ROOT / "paused_models.json"
BATCH_STATUS = ROOT / "batch_status.json"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def alert(msg):
    """语音 + 系统通知报警"""
    safe = msg.replace("'", "")
    os.system(f'say "{safe}" &')
    os.system(
        f"osascript -e 'display notification \"{safe}\" with title \"模型巡检\" sound name \"Glass\"' 2>/dev/null &"
    )


def load_paused():
    try:
        return json.load(open(PAUSED_FILE, encoding="utf-8"))
    except Exception:
        return []


def write_paused(paused):
    tmp = PAUSED_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(paused, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PAUSED_FILE)


def load_state():
    """读取跨巡检持久化状态（busy_since / silent_restart_at）"""
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {"busy_since": {}, "silent_restart_at": {}}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def restart_service(mid):
    """重启 launchd 服务（拉起浏览器）"""
    try:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.laya.{mid}"],
            timeout=15,
        )
        return True
    except Exception as e:
        log(f"  ⚠️ 重启 {mid} 服务失败: {e}")
        return False


def check_models(paused):
    """检查各模型健康，返回问题列表"""
    issues = []
    state = load_state()
    busy_since = state.setdefault("busy_since", {})
    silent_at = state.setdefault("silent_restart_at", {})
    now = time.time()
    for mid, port in MODEL_URLS.items():
        try:
            r = requests.get(
                f"http://127.0.0.1:{port}", headers={"X-API-Key": API_KEY}, timeout=10
            )
            d = r.json()
            alive = d.get("browser_alive")
            login = d.get("logged_in")
            captcha = d.get("captcha_detected")
            busy = d.get("busy")
            last_ok = d.get("last_success_at")
            log(
                f"  {mid}: alive={alive} login={login} captcha={captcha} "
                f"busy={busy} last={last_ok}"
            )

            # busy 卡死检测（状态持久化，跨巡检生效）：
            # 连续 busy 超过阈值（正常回答最长约260s）→ 疑似卡死，强制重启
            if busy:
                if mid not in busy_since:
                    busy_since[mid] = now
                if now - busy_since[mid] > BUSY_STUCK_SEC:
                    log(f"  ⚠️ {mid} busy 已超过 {BUSY_STUCK_SEC}s，疑似卡死，强制重启")
                    busy_since.pop(mid, None)
                    if restart_service(mid):
                        alert(f"{mid} 疑似卡死已自动重启")
                    continue
            else:
                busy_since.pop(mid, None)

            if mid in paused:
                # 暂停中：验证已解除且登录正常 → 自动恢复
                if not busy and login and captcha is False:
                    paused.remove(mid)
                    write_paused(paused)
                    msg = f"{mid} 人工验证已解除，自动恢复参与跑批"
                    log(f"  🔄 {msg}")
                    alert(msg)
            else:
                # 未暂停
                if not alive:
                    issues.append(f"{mid} 浏览器不可用（alive=False）")
                    # 服务进程可能活着但浏览器异常 → 重启服务
                    log(f"  ⚠️ {mid} alive=False，尝试重启服务")
                    if restart_service(mid):
                        alert(f"{mid} 浏览器异常已自动重启")
                elif busy:
                    log(f"  ⏳ {mid} 正在回答中（busy），跳过检查")
                elif captcha:
                    # 检测到验证但未暂停 → 加入暂停 + 报警（等用户处理）
                    paused.append(mid)
                    write_paused(paused)
                    msg = f"{mid} 出现人工验证，已自动暂停并等待处理"
                    log(f"  ⚠️ {msg}")
                    alert(msg)
                elif not login:
                    # 未登录：尝试自动切换备用账号（DeepSeek 等配置了 ACCOUNT_PROFILES）
                    # 切换成功（新账号已登录）→ 解除暂停、恢复跑批；失败 → 暂停等人工登录
                    log(f"  ⚠️ {mid} 未登录（login=False），尝试自动切换备用账号")
                    try:
                        sr = requests.post(
                            f"http://127.0.0.1:{port}/switch_account",
                            headers={"X-API-Key": API_KEY},
                            timeout=200,
                        )
                        sd = sr.json()
                        if sd.get("switched"):
                            issues.append(f"{mid} 未登录，已自动切换备用账号")
                            alert(f"{mid} 未登录，已自动切换备用账号")
                        else:
                            if mid not in paused:
                                paused.append(mid)
                                write_paused(paused)
                            issues.append(f"{mid} 未登录且备用账号不可用，已暂停等待人工登录")
                            log(f"  ⚠️ {mid} 未登录且无可用备用账号，已暂停等待处理")
                            alert(f"{mid} 未登录且备用账号不可用，已暂停等待处理")
                    except requests.RequestException as e:
                        issues.append(f"{mid} 切换账号调用失败: {str(e)[:60]}")
                        log(f"  ⚠️ {mid} /switch_account 调用失败: {e}")
                elif last_ok:
                    # 静默失效检测：曾成功过但长时间无新成功，且模型看似正常
                    # （alive/login 正常、无验证码、不忙碌）→ 页面可能已损坏但无报错，
                    # 自动重启服务一次恢复，避免"文心一直失败却无人处理"类场景。
                    try:
                        last_dt = datetime.fromisoformat(last_ok)
                        hours = (now - last_dt.timestamp()) / 3600
                    except Exception:
                        hours = 0
                    if hours > SILENT_HOURS and now - silent_at.get(mid, 0) > SILENT_COOLDOWN:
                        silent_at[mid] = now
                        log(f"  ⚠️ {mid} 已 {hours:.1f} 小时无成功提问（last={last_ok[:19]}），疑似静默失效，自动重启")
                        issues.append(f"{mid} 长时间无成功提问，已自动重启")
                        if restart_service(mid):
                            alert(f"{mid} 长时间无成功提问，已自动重启")
        except requests.RequestException as e:
            issues.append(f"{mid} 服务无响应: {str(e)[:80]}")
            log(f"  ⚠️ {mid} 服务无响应，尝试重启")
            if restart_service(mid):
                alert(f"{mid} 服务无响应已自动重启")
        except Exception as e:
            issues.append(f"{mid} 检查异常: {str(e)[:80]}")
            log(f"  ⚠️ {mid} 检查异常: {e}")
    save_state(state)
    return issues


def check_runners():
    """检查主 runner 与 fix_runner 是否存活、跑批是否停滞"""
    issues = []
    for name, pat in (("主runner", "minsheng_batch_runner.py"),
                      ("fix_runner", "fix_runner.py")):
        r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
        if not r.stdout.strip():
            log(f"  ⚠️ {name} 进程不存在（watchdog 将自动拉起）")
        else:
            log(f"  ✅ {name} 存活 PID: {r.stdout.strip().splitlines()[:2]}")

    # 跑批停滞检查：batch_status 最近提问时间
    try:
        st = json.load(open(BATCH_STATUS, encoding="utf-8"))
        recent = st.get("recent") or []
        if recent:
            last_ts = recent[0].get("time") or recent[0].get("timestamp") or ""
            log(f"  最近提问: {last_ts}（{recent[0].get('question', '')[:30]}）")
            # 若超过 25 分钟无新题且主 runner 存活 → 疑似卡住，报警提示
            try:
                last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                now = datetime.now(last_dt.tzinfo)
                gap_min = (now - last_dt).total_seconds() / 60
                if gap_min > 25:
                    issues.append(f"跑批 {gap_min:.0f} 分钟无新提问，疑似停滞")
                    log(f"  ⚠️ 跑批 {gap_min:.0f} 分钟无新提问")
                    alert(f"跑批 {gap_min:.0f} 分钟无新提问，请检查")
            except Exception:
                pass
        else:
            log("  batch_status 无 recent 数据")
    except Exception as e:
        log(f"  ⚠️ 读取 batch_status 失败: {e}")
    return issues


def main():
    log("=" * 50)
    log("🔍 模型巡检开始")
    paused = load_paused()
    log(f"  当前暂停: {paused}")
    issues = check_models(paused)
    issues += check_runners()
    if issues:
        log(f"⚠️ 本轮发现问题 {len(issues)} 项：")
        for i in issues:
            log(f"  - {i}")
    else:
        log("✅ 全部正常")
    log("巡检结束")


if __name__ == "__main__":
    main()
