#!/usr/bin/env python3
"""内存守护 v2：预防式轮换重启 + 紧急兜底。

策略（根治 Chrome 长跑累积）：
1. 预防式：距上次重启 >= ROTATE_INTERVAL（2小时）时，主动重启轮换列表中
   下一个非 busy 模型 —— 每个模型 Chrome 最多跑约 10 小时就被重启一轮，
   内存峰值被锁死，不再出现"释放后又涨满"。
2. 紧急式：内存 >= URGENT_THRESHOLD（90%）时，立即重启占用最大的非 busy
   模型 —— 防止突发峰值。
3. 重启瞬间该模型 1-2 题提问会失败，由 fix_runner 补齐（占比 <2%）。
登录态随 Chrome 配置文件保留，无需重新扫码。

由 launchd 每 5 分钟触发一次。
"""
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

URGENT_THRESHOLD = 90    # 紧急阈值 %
ROTATE_INTERVAL = 2 * 3600   # 预防式轮换间隔：2小时
URGENT_COOLDOWN = 15 * 60    # 紧急重启后冷却 15 分钟
TOTAL_MEM_GB = 32
LOG = Path("/tmp/memory_guard.log")
STATE_FILE = Path(__file__).parent / "memory_guard_state.json"
API_KEY = "laya-local-model-key"

MODELS = ["deepseek", "qianwen", "doubao", "wenxin", "yuanbao"]
PORTS = {"deepseek": 8000, "qianwen": 8001, "doubao": 8002,
         "wenxin": 8003, "yuanbao": 8004}

# ---- 日志轮转：防止 24×7 长期运行磁盘膨胀 ----
LOG_RETENTION_DAYS = 7          # server_logs/*.jsonl 保留天数
LOG_MAX_MB = 100                # /tmp 核心日志单文件上限（超限截断保留尾部）
CLEANUP_MARKER = Path("/tmp/.laya_log_cleanup_date")


def cleanup_logs():
    """每日一次：清理过期 jsonl 日志、截断超大 /tmp 日志。launchd 每 5 分钟触发，
    用日期标记文件保证每天只执行一次。"""
    try:
        today = time.strftime("%Y-%m-%d")
        if CLEANUP_MARKER.exists() and CLEANUP_MARKER.read_text().strip() == today:
            return
        logs_dir = Path(__file__).parent / "server_logs"
        if logs_dir.is_dir():
            for f in logs_dir.glob("*.jsonl"):
                try:
                    if (time.time() - f.stat().st_mtime) / 86400 > LOG_RETENTION_DAYS:
                        f.unlink()
                except Exception:
                    pass
        for p in ["/tmp/batch_run.log", "/tmp/fix_run.log", "/tmp/model_watch.log",
                  "/tmp/batch_watchdog.log", "/tmp/memory_guard.log"]:
            f = Path(p)
            try:
                if f.exists() and f.stat().st_size > LOG_MAX_MB * 1024 * 1024:
                    tmp = f.with_suffix(".log.tmp")
                    subprocess.run(["tail", "-n", "5000", str(f)],
                                   stdout=open(tmp, "w", encoding="utf-8"), timeout=60)
                    os.replace(tmp, f)
            except Exception:
                pass
        CLEANUP_MARKER.write_text(today)
    except Exception:
        pass


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_mem_pct() -> int:
    """活动监视器口径：used = active + wired + compressed（不含可回收 inactive 缓存）。
    避免 top 的 PhysMem used 含 inactive 缓存导致长期虚高（如真实 56% 显示 90%），
    从而误触紧急重启浏览器。"""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=30).stdout

        def gb(key: str) -> float:
            m = re.search(rf"{key}:\s+(\d+)", out)
            return int(m.group(1)) * 4096 / (1024 ** 3) if m else 0.0

        used = gb("Pages active") + gb("Pages wired down") + gb("Pages occupied by compressor")
        if used > 0:
            return int(used * 100 // TOTAL_MEM_GB)
    except Exception:
        pass
    return 0


def chrome_mem_mb(mid: str) -> int:
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True,
                             text=True, timeout=15).stdout
        total = 0
        for line in out.splitlines():
            if "Google Chrome" in line and "user-data-dir=" in line \
                    and f"/{mid}" in line.split("user-data-dir=")[-1].split('"')[0]:
                try:
                    total += int(line.split()[5])
                except Exception:
                    pass
        return total // 1024
    except Exception:
        return 0


def runner_busy() -> bool:
    """主 runner 是否正在提问：batch_status.json 的 status == "running"。
    主 runner 每 10 题休息时置为 "resting"，提问期间为 "running"；
    running 期间是并发 gather（所有模型同时被 ask），重启任何模型都会
    打断未完成的 ask、丢失该题结果，因此一律视为 busy，绝不重启。
    resting 期间模型全部空闲，是安全的重启窗口。"""
    try:
        d = json.loads(
            (Path(__file__).parent / "batch_status.json")
            .read_text(encoding="utf-8"))
        return d.get("status") == "running"
    except Exception:
        return False  # 读不到状态时交给模型级 busy 检查兜底


def is_busy(mid: str) -> bool:
    # 主 runner 正在提问 → 全部模型视为 busy，绝不打断
    if runner_busy():
        return True
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORTS[mid]}/",
            headers={"X-API-Key": API_KEY})
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read())
            return bool(d.get("busy"))
    except Exception:
        return True


def restart_model(mid: str) -> bool:
    log(f"🔁 重启 {mid} 释放内存...")
    r = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.laya.{mid}"],
        capture_output=True, text=True, timeout=60)
    ok = r.returncode == 0
    log(f"  {'✅' if ok else '❌'} {mid} 重启{'成功' if ok else '失败: ' + r.stderr[:120]}")
    return ok


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_restart": 0, "last_mid": None, "last_ok": None,
                 "last_kind": None}


def save_state(s: dict):
    try:
        STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def next_in_rotation(last_mid):
    """轮换顺序：从上次重启的模型之后开始。"""
    if last_mid and last_mid in MODELS:
        i = MODELS.index(last_mid)
        return MODELS[i + 1:] + MODELS[:i + 1]
    return MODELS


def pick_target(mems, prefer_rotation, last_mid):
    """选目标：预防式优先轮换顺序，紧急式优先占用最大。跳过 busy。"""
    order = next_in_rotation(last_mid) if prefer_rotation else \
        [m for m, _ in sorted(mems, key=lambda x: -x[1])]
    for mid in order:
        mem = dict(mems).get(mid, 0)
        if is_busy(mid):
            log(f"  ⏭️ {mid} busy（提问中），跳过")
            continue
        return (mid, mem)
    return None


def main():
    dry = "--check" in os.sys.argv
    cleanup_logs()  # 每日一次日志轮转（磁盘卫生）
    mem = get_mem_pct()
    state = load_state()
    now = time.time()
    elapsed = now - state.get("last_restart", 0)
    last_kind = state.get("last_kind")
    last_mid = state.get("last_mid")

    # 紧急式：内存超阈值，立即处理（尊重紧急冷却）
    if mem >= URGENT_THRESHOLD:
        if elapsed < URGENT_COOLDOWN:
            left = int((URGENT_COOLDOWN - elapsed) / 60)
            log(f"⚠️ 内存 {mem}% 超紧急阈值，但距上次重启 {left} 分钟（冷却中），跳过")
            return
        mems = [(m, chrome_mem_mb(m)) for m in MODELS]
        target = pick_target(mems, prefer_rotation=False, last_mid=last_mid)
        if not target:
            log(f"⚠️ 内存 {mem}% 超阈值但所有模型都 busy，本轮跳过")
            return
        mid, m = target
        log(f"🚨 紧急：内存 {mem}% → 重启 {mid}（占用 {m}MB）")
        if dry:
            print(f"[dry-run 紧急] 将重启 {mid}（{m}MB）")
            return
        ok = restart_model(mid)
        state.update({"last_restart": now, "last_mid": mid, "last_ok": ok,
                      "last_kind": "urgent", "mem_at": mem, "freed_mb": m})
        save_state(state)
        return

    # 预防式：距上次轮换够久，主动重启下一个（无论内存多少）
    if elapsed >= ROTATE_INTERVAL:
        mems = [(m, chrome_mem_mb(m)) for m in MODELS]
        target = pick_target(mems, prefer_rotation=True, last_mid=last_mid)
        if not target:
            log(f"ℹ️ 轮换期到但所有模型都 busy，下轮再试")
            return
        mid, m = target
        log(f"🔄 预防式轮换：距上次 {int(elapsed/3600)}h → 重启 {mid}（占用 {m}MB）")
        if dry:
            print(f"[dry-run 预防] 将重启 {mid}（{m}MB）")
            return
        ok = restart_model(mid)
        state.update({"last_restart": now, "last_mid": mid, "last_ok": ok,
                      "last_kind": "rotate", "mem_at": mem, "freed_mb": m})
        save_state(state)
        return

    # 正常：内存健康且未到轮换期
    if dry:
        print(f"[ok] 内存 {mem}%，距上次重启 {int(elapsed/60)} 分钟，无需动作")
    else:
        log(f"✅ 内存 {mem}%，距上次 {int(elapsed/60)} 分钟，健康")


if __name__ == "__main__":
    main()
