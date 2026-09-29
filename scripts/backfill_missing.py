#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补齐历史结果文件中缺失/失败的模型数据（如千问暂停期未参与）。

用法: .venv39/bin/python3 scripts/backfill_missing.py
节奏: 题间15-25秒随机, DeepSeek 60-120秒降速, 每10题休息5-10分钟。
"""
import asyncio, glob, json, os, random, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minsheng_batch_runner import (
    MODELS_ALL, active_models, call_model,
    DS_DELAY_MIN, DS_DELAY_MAX, DELAY_MIN, DELAY_MAX,
    REST_EVERY, REST_MIN, REST_MAX,
)

OUT_DIR = ROOT / "民生行业跑批结果"
LOG_FILE = "/tmp/backfill.log"

def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def find_missing_files():
    """返回 [(文件路径, [缺失模型id], 问题文本)] 列表。"""
    result = []
    active_ids = [m["id"] for m in active_models()]
    for f in sorted(glob.glob(str(OUT_DIR / "**" / "*.json"), recursive=True)):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        have = set((d.get("results") or {}).keys())
        lack = [mid for mid in active_ids if mid not in have]
        # 也补 results 里 status!=ok 的（错误状态）
        bad = [mid for mid, r in (d.get("results") or {}).items() if r.get("status") != "ok"]
        need = sorted(set(lack + bad))
        if need:
            result.append((f, need, d.get("question") or d.get("prompt") or ""))
    return result

async def run():
    files = find_missing_files()
    log(f"扫描完成：{len(files)} 个文件需要补齐")
    done = 0
    for i, (fpath, need_ids, qtext) in enumerate(files, 1):
        # 断点：重新检查（避免重复补已补好的）
        with open(fpath, encoding="utf-8") as fh:
            d = json.load(fh)
        have = set((d.get("results") or {}).keys())
        bad = [mid for mid, r in (d.get("results") or {}).items() if r.get("status") != "ok"]
        active_ids = [m["id"] for m in active_models()]
        still_need = sorted(set([mid for mid in active_ids if mid not in have] + bad))
        if not still_need:
            continue

        qtext = (d.get("prompt_text") or d.get("question") or d.get("prompt") or "").strip()
        if not qtext:
            log(f"  ⚠️ 跳过：文件无问题文本 {os.path.basename(fpath)}")
            continue
        log(f"[{i}/{len(files)}] 补齐 {os.path.basename(fpath)[:40]}... 缺: {still_need}")

        # 并发问缺失模型（DeepSeek降速）
        targets = [m for m in active_models() if m["id"] in still_need]
        async def _ask(m):
            try:
                if m["id"] == "deepseek":
                    await asyncio.sleep(random.uniform(DS_DELAY_MIN, DS_DELAY_MAX))
                return m["id"], await asyncio.get_event_loop().run_in_executor(None, call_model, m, qtext)
            except Exception as e:
                return m["id"], {
                    "status": "error", "error": str(e)[:200],
                    "answer": "", "search_queries": [], "citations": [],
                    "asked_at": datetime.now().astimezone().isoformat(),
                }
        new_results = dict(await asyncio.gather(*[_ask(m) for m in targets]))

        # 合并写回（保留原有ok结果，覆盖失败/缺失）
        results = dict(d.get("results") or {})
        for mid, r in new_results.items():
            results[mid] = r
        d["results"] = results
        d["updated_at"] = datetime.now().astimezone().isoformat()

        tmp = fpath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, fpath)

        bad_now = [mid for mid, r in results.items() if r.get("status") != "ok"]
        done += 1
        if bad_now:
            log(f"  ⚠️ 仍有失败: {bad_now}（下次补齐重试）")
        else:
            log(f"  ✅ 补齐完成")

        # 防风控节奏
        if done % REST_EVERY == 0:
            rest = random.randint(REST_MIN, REST_MAX)
            log(f"🛌 已补齐 {done} 个，休息 {rest//60} 分钟...")
            await asyncio.sleep(rest)
        else:
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            log(f"⏳ 等待 {delay:.1f} 秒...")
            await asyncio.sleep(delay)

    log(f"=== 补齐结束：处理 {done} 个文件 ===")

if __name__ == "__main__":
    asyncio.run(run())
