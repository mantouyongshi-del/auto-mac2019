#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缺口补齐进程（双轨并行）：
主跑批(minsheng_batch_runner.py)持续跑新题；本进程专职补历史缺口（缺失模型的回答），
互不阻塞。当前目标模型：qianwen（千问，3分钟/题节奏）；deepseek 解封后加入。

用法: nohup .venv39/bin/python3 fix_runner.py >> /tmp/fix_run.log 2>&1 &
"""
import asyncio
import atexit
import glob
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "民生行业跑批结果"
FIX_STATUS = ROOT / "fix_status.json"
LOCK_FILE = ROOT / "fix_runner.lock"
MODEL_API_KEY = os.environ.get("LAYA_API_KEY", "laya-local-model-key")
MODEL_URLS = {
    "deepseek": "http://127.0.0.1:8000",
    "qianwen": "http://127.0.0.1:8001",
    "doubao": "http://127.0.0.1:8002",
    "wenxin": "http://127.0.0.1:8003",
    "yuanbao": "http://127.0.0.1:8004",
}
QW_DELAY_MIN, QW_DELAY_MAX = 180, 210   # 千问节奏（与主跑批一致）
DS_DELAY_MIN, DS_DELAY_MAX = 150, 210   # deepseek 节奏（2026-10-02 再拉长一档防风控，150-210s）


def batch_paused():
    """整批暂停标记（mac-monitor 控制端写入 pause_batch.json）。"""
    try:
        with open(ROOT / "pause_batch.json", encoding="utf-8") as f:
            return json.load(f).get("paused", False)
    except Exception:
        return False


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_paused():
    try:
        return json.load(open(ROOT / "paused_models.json", encoding="utf-8"))
    except Exception:
        return []


def load_pause_reasons():
    """读暂停原因 map（captcha/quota_exhausted/switch_failed）。缺失视为 captcha。"""
    try:
        return json.load(open(ROOT / "paused_reasons.json", encoding="utf-8"))
    except Exception:
        return {}


def quota_reset_today(mid: str) -> bool:
    """额度用尽类暂停是否仍处当天（未跨天）。true=仍当天，保持暂停；false=跨天已重置，可恢复。"""
    try:
        d = json.load(open(f"/tmp/laya_{mid}_account_usage.json", encoding="utf-8"))
        return d.get("date") == datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return True  # 拿不到计数文件则保守保持暂停


def write_fix_status(data):
    try:
        tmp = FIX_STATUS.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FIX_STATUS)
    except Exception:
        pass


def note_recent(mid: str, question: str, ok: bool, r: dict):
    """补缺提问记录写入 recent_fix.json（驾驶舱合并展示，不区分主跑批/补缺）。"""
    try:
        import fcntl
        path = ROOT / "recent_fix.json"
        try:
            fh = open(path, encoding="utf-8")
        except FileNotFoundError:
            d = {"items": []}
            fh = None
        if fh is not None:
            with fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    d = json.load(fh)
                except Exception:
                    d = {"items": []}
        d.setdefault("items", []).insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"),
            "no": None,
            "question": (question or "")[:40],
            "failed": [] if ok else [mid],
            "models": [mid],
            "source": "fix",
            "cite": len(r.get("citations", [])),
            "queries": len(r.get("search_queries", [])),
        })
        d["items"] = d["items"][:50]
        # 模型统计：补缺成功/失败计入 run_ok/run_err（dashboard 合并展示）
        stats = d.setdefault("stats", {})
        mstat = stats.setdefault(mid, {"run_ok": 0, "run_err": 0})
        mstat["run_ok" if ok else "run_err"] = mstat.get("run_ok" if ok else "run_err", 0) + 1
        mstat["last_ask"] = datetime.now().astimezone().isoformat()
        mstat["last_result"] = "ok" if ok else "error"
        d["stats"] = stats
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:
        pass


def call_model(mid: str, question: str) -> dict:
    """调用模型 /ask 接口，返回统一结果字典。"""
    url = MODEL_URLS.get(mid)
    if not url:
        return {"status": "error", "error": f"no url for {mid}", "answer": "",
                "search_queries": [], "citations": [],
                "asked_at": datetime.now().astimezone().isoformat()}
    try:
        r = requests.post(f"{url}/ask", json={"question": question},
                          headers={"X-API-Key": MODEL_API_KEY}, timeout=330)
        if r.status_code == 429:
            # 429（冷却/配额已尽/切换失败等）一律按 cooldown 跳过：不计数失败，
            # 保留缺口等恢复后自然补上；模型自身会维护暂停/恢复状态。
            return {"status": "cooldown", "error": "模型暂不可用（429），跳过本轮",
                    "answer": "", "search_queries": [], "citations": [],
                    "asked_at": datetime.now().astimezone().isoformat()}
        if r.status_code != 200:
            return {"status": "error", "error": f"HTTP {r.status_code}",
                    "answer": "", "search_queries": [], "citations": [],
                    "asked_at": datetime.now().astimezone().isoformat()}
        d = r.json()
        return {
            "status": "ok",
            "answer": d.get("answer", ""),
            "search_queries": d.get("search_queries", []),
            "citations": d.get("citations", []),
            "asked_at": datetime.now().astimezone().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:200], "answer": "",
                "search_queries": [], "citations": [],
                "asked_at": datetime.now().astimezone().isoformat()}


def scan_missing(target_models: list) -> list:
    """扫描结果目录，返回缺失目标模型的文件列表 [(path, mid, question)]。
    跳过修改时间 < 90 秒的文件（避免与主跑批正在写入的新题文件竞争）。"""
    missing = []
    cutoff = time.time() - 90
    for f in sorted(glob.glob(str(OUT_DIR / "*" / "*.json"))):
        try:
            if os.path.getmtime(f) > cutoff:
                continue  # 刚写的新题文件，等主循环写完稳定后再补
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        res = d.get("results") or {}
        question = d.get("prompt_text") or d.get("question") or ""
        for mid in target_models:
            if mid not in res:
                missing.append((f, mid, question))
    return missing


def merge_write(path: str, mid: str, r: dict):
    """读原文件，合并该模型结果，原子写回。"""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        log(f"  ⚠️ 读取失败跳过: {path}")
        return False
    res = d.setdefault("results", {})
    res[mid] = r
    d["results"] = res
    tmp = path + ".fix.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return True


async def run_once(target_models: list, status: dict):
    missing = scan_missing(target_models)
    total = len(missing)
    done = status.get("done", 0)
    status.update({"pending": total, "updated_at": datetime.now().astimezone().isoformat()})
    write_fix_status(status)
    if total == 0:
        if done > 0:
            status["qianwen_done"] = True
            status["done_at"] = datetime.now().astimezone().isoformat()
            write_fix_status(status)
            log("🎉 千问缺口全部补齐，标记归队；若后续产生新缺口将自动继续补。")
        return 0

    status["qianwen_done"] = False
    write_fix_status(status)

    # 每轮配额：qianwen 与其他模型并行补（千问 3 分钟节奏、其余 15-25 秒，互不阻塞）
    qw_items = [x for x in missing if x[1] == "qianwen"][:5]
    other_items = [x for x in missing if x[1] != "qianwen"][:5]
    tasks = []
    if other_items:
        tasks.append(_run_pick(other_items, status))
    if qw_items:
        tasks.append(_run_pick(qw_items, status))
    if tasks:
        done_add = sum(await asyncio.gather(*tasks))
        if done_add:
            status["done"] = status.get("done", 0) + done_add
            status["pending"] = max(0, status.get("pending", 0) - done_add)
            status["last_success"] = datetime.now().astimezone().isoformat()
            write_fix_status(status)
    return 1


async def _run_pick(pick_items: list, status: dict) -> int:
    """补一组缺口（同一模型节奏内部串行，多模型之间并行）。返回成功题数。"""
    ok_count = 0
    for path, mid, question in pick_items:
        if not question:
            continue
        # 节奏延迟（模型专属）：千问 3 分钟、deepseek 60-120s、其余 15-25s
        if mid == "qianwen":
            await asyncio.sleep(random.uniform(QW_DELAY_MIN, QW_DELAY_MAX))
        elif mid == "deepseek":
            await asyncio.sleep(random.uniform(DS_DELAY_MIN, DS_DELAY_MAX))
        else:
            await asyncio.sleep(random.uniform(15, 25))

        log(f"  🔧 [{mid}] 补齐: {Path(path).parent.name}/{Path(path).name[:40]} → {question[:30]}")
        r = await asyncio.get_event_loop().run_in_executor(None, call_model, mid, question)
        if r.get("status") == "cooldown":
            # 模型强制休息中：跳过该缺口，等冷却结束后下轮自然补上（不计数失败）
            log(f"  ⏳ {mid} 强制休息中，缺口保留待冷却后补齐")
            continue
        ok = r.get("status") == "ok"
        log(f"  {'✅' if ok else '❌'} {mid} 结果: {'成功' if ok else '失败'} 引用{len(r.get('citations', []))} 搜索词{len(r.get('search_queries', []))}")
        note_recent(mid, question, ok, r)
        if ok:
            merge_write(path, mid, r)
            ok_count += 1
        else:
            status["last_error"] = {"mid": mid, "time": datetime.now().astimezone().isoformat(),
                                    "error": str(r.get("error"))[:150]}
            write_fix_status(status)
            log(f"  ⏸️ {mid} 失败，保留缺口等待下轮重试")
    return ok_count


def write_paused(paused: list):
    tmp = ROOT / "paused_models.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(paused, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ROOT / "paused_models.json")


async def auto_resume_check():
    """每 5 分钟检查被暂停模型：人工验证已解除且登录正常 → 自动恢复（写回 paused 列表）。
    区分两种情况：验证未解除（保持暂停，等用户处理）vs 用户已解除（自动继续）。"""
    while True:
        await asyncio.sleep(300)
        try:
            paused = load_paused()
            if not paused:
                continue
            log(f"🔍 自动恢复巡检：检查暂停模型 {paused}")
            reasons = load_pause_reasons()
            for mid in paused[:]:
                url = MODEL_URLS.get(mid)
                if not url:
                    continue
                reason = reasons.get(mid, "captcha")
                # 额度用尽类：不按登录态恢复，只有跨天（usage 日期重置）后才解除暂停
                if reason == "quota_exhausted":
                    if quota_reset_today(mid):
                        log(f"  ⏸️ {mid} 当日额度已用尽（quota_exhausted），保持暂停等待次日")
                        continue
                    paused.remove(mid)
                    write_paused(paused)
                    log(f"🔄 自动恢复 {mid}：新的一天额度已重置，解除额度暂停")
                    os.system(f'say "模型{mid}额度已恢复" &')
                    continue
                try:
                    r = requests.get(url, headers={"X-API-Key": MODEL_API_KEY}, timeout=10)
                    d = r.json()
                    if d.get("busy"):
                        log(f"  ⏳ {mid} 正在回答中，跳过本轮巡检")
                        continue
                    if d.get("logged_in") and d.get("captcha_detected") is False:
                        paused.remove(mid)
                        write_paused(paused)
                        log(f"🔄 自动恢复 {mid}：人工验证已解除、登录正常，恢复参与提问")
                        os.system(f'say "模型{mid}已自动恢复" &')
                        os.system("osascript -e 'display notification \"人工验证已解除，模型已自动恢复\" with title \"跑批自动恢复\" sound name \"Glass\"' 2>/dev/null &")
                    else:
                        log(f"  ⏸️ {mid} 验证未解除或登录异常（login={d.get('logged_in')}, captcha={d.get('captcha_detected')}），保持暂停")
                except Exception as e:
                    log(f"  ⚠️ {mid} 巡检失败: {e}")
        except Exception as e:
            log(f"⚠️ 自动恢复巡检异常: {e}")


def acquire_lock() -> bool:
    """单实例锁：已存在且进程存活则拒绝启动；退出时自动清理锁。"""
    try:
        if LOCK_FILE.exists():
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # 存活则占用
            return False
        LOCK_FILE.write_text(str(os.getpid()))
        atexit.register(lambda: _release_lock())
        return True
    except (ProcessLookupError, ValueError):
        LOCK_FILE.write_text(str(os.getpid()))
        atexit.register(lambda: _release_lock())
        return True
    except Exception:
        return True


def _release_lock():
    """退出时删除锁文件（仅当锁是自己的 PID）。"""
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except Exception:
        pass


async def main():
    if not acquire_lock():
        log("⚠️ fix_runner 已在运行，退出。")
        sys.exit(0)
    log("🚀 补齐进程启动（双轨并行，目标: qianwen）")
    status = {"started_at": datetime.now().astimezone().isoformat(),
              "done": 0, "pending": 0, "qianwen_done": False}
    asyncio.create_task(auto_resume_check())  # 后台自动恢复巡检
    while True:
        try:
            # 控制端暂停：不补缺，等恢复
            while batch_paused():
                log("⏸️ 补缺已暂停（控制端），等待恢复...")
                await asyncio.sleep(30)
            paused = load_paused()
            targets = [m for m in MODEL_URLS if m not in paused]
            await run_once(targets, status)
        except Exception as e:
            log(f"⚠️ 循环异常: {e}")
        await asyncio.sleep(60)  # 每 60 秒重新扫描


if __name__ == "__main__":
    asyncio.run(main())
