#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缺口补齐进程（双轨并行）：
主跑批(minsheng_batch_runner.py)持续跑新题；本进程专职补历史缺口（缺失模型的回答），
互不阻塞。当前目标模型：qianwen（千问，3分钟/题节奏）；deepseek 解封后加入。

用法: nohup .venv39/bin/python3 fix_runner.py >> /tmp/fix_run.log 2>&1 &
"""
import asyncio
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
MODEL_URLS = {"qianwen": "http://127.0.0.1:8001", "deepseek": "http://127.0.0.1:8000"}
QW_DELAY_MIN, QW_DELAY_MAX = 180, 210   # 千问节奏（与主跑批一致）
DS_DELAY_MIN, DS_DELAY_MAX = 60, 120    # deepseek 节奏


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_paused():
    try:
        return json.load(open(ROOT / "paused_models.json", encoding="utf-8"))
    except Exception:
        return []


def write_fix_status(data):
    try:
        tmp = FIX_STATUS.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FIX_STATUS)
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
        for mid in target_models:
            if mid not in res:
                question = d.get("prompt_text") or d.get("question") or ""
                missing.append((f, mid, question))
                break
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

    for path, mid, question in missing[:10]:  # 每轮最多补 10 题，之后重新扫描（防止阻塞主循环新缺口）
        if not question:
            continue
        # 节奏延迟（模型专属）
        if mid == "qianwen":
            await asyncio.sleep(random.uniform(QW_DELAY_MIN, QW_DELAY_MAX))
        elif mid == "deepseek":
            await asyncio.sleep(random.uniform(DS_DELAY_MIN, DS_DELAY_MAX))
        else:
            await asyncio.sleep(random.uniform(15, 25))

        log(f"  🔧 [{mid}] 补齐: {Path(path).parent.name}/{Path(path).name[:40]} → {question[:30]}")
        r = await asyncio.get_event_loop().run_in_executor(None, call_model, mid, question)
        ok = r.get("status") == "ok"
        log(f"  {'✅' if ok else '❌'} {mid} 结果: {'成功' if ok else '失败'} 引用{len(r.get('citations', []))} 搜索词{len(r.get('search_queries', []))}")
        if ok:
            merge_write(path, mid, r)
            status["done"] = status.get("done", 0) + 1
            status["pending"] = max(0, status.get("pending", 1) - 1)
            status["last_success"] = datetime.now().astimezone().isoformat()
            write_fix_status(status)
        else:
            status["last_error"] = {"mid": mid, "time": datetime.now().astimezone().isoformat(),
                                    "error": str(r.get("error"))[:150]}
            write_fix_status(status)
            log(f"  ⏸️ {mid} 失败，保留缺口等待下轮重试")
    return 1


def acquire_lock() -> bool:
    """单实例锁：已存在且进程存活则拒绝启动。"""
    try:
        if LOCK_FILE.exists():
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # 存活则占用
            return False
        LOCK_FILE.write_text(str(os.getpid()))
        return True
    except (ProcessLookupError, ValueError):
        LOCK_FILE.write_text(str(os.getpid()))
        return True
    except Exception:
        return True


async def main():
    if not acquire_lock():
        log("⚠️ fix_runner 已在运行，退出。")
        sys.exit(0)
    log("🚀 补齐进程启动（双轨并行，目标: qianwen）")
    status = {"started_at": datetime.now().astimezone().isoformat(),
              "done": 0, "pending": 0, "qianwen_done": False}
    while True:
        try:
            paused = load_paused()
            targets = [m for m in MODEL_URLS if m not in paused]
            await run_once(targets, status)
        except Exception as e:
            log(f"⚠️ 循环异常: {e}")
        await asyncio.sleep(60)  # 每 60 秒重新扫描


if __name__ == "__main__":
    asyncio.run(main())
