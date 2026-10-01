#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 v3 跑批结果中文心抓取到的"搜索过程+资料列表"过程文本，重问后写回真实回答。

用法: .venv39/bin/python3 fix_wenxin_data.py [行业目录关键词]
示例: .venv39/bin/python3 fix_wenxin_data.py 101_冒菜
"""
import json, sys, time, random, urllib.request
from pathlib import Path

BASE = Path("/Users/alili/laya")
RESULTS = BASE / "民生行业跑批结果"
MODEL_URL = "http://127.0.0.1:8003/ask"
API_KEY = "laya-local-model-key"

PROCESS_MARKERS = ("搜索", "共参考", "使用工具", "搜索关键词", "搜索全网", "已搜索")


def looks_process(text: str) -> bool:
    return sum(1 for m in PROCESS_MARKERS if m in text) >= 2


def find_bad():
    bad = []
    for f in sorted(RESULTS.glob("*/*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        r = d.get("results") or {}
        wx = r.get("wenxin") or {}
        a = wx.get("answer") or ""
        if wx.get("status") == "ok" and a and looks_process(a):
            bad.append((f, d))
    return bad


def ask_question(q, retries=2):
    req = urllib.request.Request(
        MODEL_URL,
        data=json.dumps({"question": q}).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
        method="POST",
    )
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=420) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                d = json.loads(raw)
                return d.get("result") or d
        except Exception as e:
            last = {"status": "error", "error": repr(e)[:300]}
            print(f"    重试 {attempt + 1}/{retries + 1}: {last['error']}", flush=True)
            time.sleep(30)
    return last


def main():
    industry = sys.argv[1] if len(sys.argv) > 1 else None
    bad = find_bad()
    if industry:
        bad = [(f, d) for f, d in bad if industry in str(f)]
    print(f"[文心修复] 待修复: {len(bad)} 条", flush=True)
    ok = fail = 0
    for i, (f, d) in enumerate(bad, 1):
        q = d.get("prompt_text") or d.get("question")
        if not q:
            continue
        print(f"[{i}/{len(bad)}] {f.parent.name}: {q[:40]}...", flush=True)
        res = ask_question(q)
        # 模型服务 /ask 成功返回无 status 键（含 answer 即成功）
        if (res.get("status") == "ok") or (res.get("answer") or "").strip():
            r = d.setdefault("results", {})
            r["wenxin"] = {
                "status": "ok",
                "answer": res.get("answer") or "",
                "answer_html": res.get("answer_html") or "",
                "citations": res.get("citations") or [],
                "search_queries": res.get("search_queries") or [],
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            }
            json.dump(d, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            ok += 1
            print(f"  OK 长度:{len(res.get('answer') or '')} 引用:{len(res.get('citations') or [])}", flush=True)
        else:
            fail += 1
            print(f"  FAIL {res}", flush=True)
        # 问完休息 25-35 秒（比跑批慢，让跑批优先用文心）
        time.sleep(random.uniform(25, 35))
    print(f"[文心修复] 完成: 成功{ok} 失败{fail}", flush=True)


if __name__ == "__main__":
    main()
