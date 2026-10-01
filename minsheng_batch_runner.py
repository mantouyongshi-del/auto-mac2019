#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
民生行业大模型跑批引擎
========================
读取「民生行业大模型跑批」目录下的行业JSON，逐条prompt调用5个模型服务（/ask），
抓取：回答正文(answer)、搜索关键词(search_queries)、引用信源(citations)，最重要的引用。
结果按「行业目录/prompt文件」落盘到「民生行业跑批结果」目录。

防风控策略：
- 同一问题并发发5个模型（不同平台，不叠加风控）
- 两个问题之间随机间隔 15-25 秒（可配）
- 每跑 N 题休息 5-10 分钟（可配）
- 断点续跑：已落盘的结果自动跳过，不会重复提问
- 失败标记 error 继续下一个，不中断

用法：
  python3 minsheng_batch_runner.py                # 全量跑
  python3 minsheng_batch_runner.py --limit 50     # 只跑前50条（小批量试跑）
  python3 minsheng_batch_runner.py --industry 1   # 只跑指定行业ID
  python3 minsheng_batch_runner.py --resume       # 断点续跑（默认自动续跑）
"""
import os
import sys
import json
import time
import random
import argparse
import asyncio
import urllib.request
import re
from datetime import datetime
from pathlib import Path

# ---------- 配置 ----------
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "民生行业跑批_v3"
OUT_DIR = ROOT / "民生行业跑批结果"

# 模型服务（全部5个）
MODELS_ALL = [
    {"id": "deepseek", "name": "DeepSeek", "port": 8000},
    {"id": "qianwen", "name": "千问", "port": 8001},
    {"id": "doubao", "name": "豆包", "port": 8002},
    {"id": "wenxin", "name": "文心一言", "port": 8003},
    {"id": "yuanbao", "name": "腾讯元宝", "port": 8004},
]

def load_paused_models() -> set:
    """读取暂停模型（与Dashboard共用 paused_models.json，按 model_id）。"""
    pf = Path(__file__).parent / "paused_models.json"
    try:
        if pf.exists():
            return set(json.load(open(pf, encoding="utf-8")))
    except Exception:
        pass
    return set()

PAUSED_MODELS = load_paused_models()
MODELS = [m for m in MODELS_ALL if m["id"] not in PAUSED_MODELS]
if PAUSED_MODELS:
    # log() 尚未定义（定义在下方），此处用 print
    print(f"⏸️ 启动时已暂停模型: {sorted(PAUSED_MODELS)}，本轮仅使用 {len(MODELS)} 个模型: {[m['id'] for m in MODELS]}", flush=True)

def active_models():
    """每题动态读取暂停列表：额度恢复后清空 paused_models.json 即自动恢复，无需重启。"""
    paused = load_paused_models()
    return [m for m in MODELS_ALL if m["id"] not in paused]

def batch_paused():
    """整批暂停标记（mac-monitor 控制端写入 pause_batch.json）。"""
    try:
        with open(Path(__file__).parent / "pause_batch.json", encoding="utf-8") as f:
            return json.load(f).get("paused", False)
    except Exception:
        return False

MODEL_API_KEY = os.environ.get("LAYA_API_KEY", "laya-local-model-key")

# 防风控节奏
DELAY_MIN = 15      # 题间最小间隔（秒）
DELAY_MAX = 25      # 题间最大间隔（秒）
# DeepSeek 专属降速（该平台风控敏感，恢复后27题即触发封禁，需拉长提问间隔）
DS_DELAY_MIN = 90    # DeepSeek 额外延迟下限（秒，2026-10-02 再拉长一档防风控）
DS_DELAY_MAX = 150   # DeepSeek 额外延迟上限（秒）
QW_DELAY_MIN = 180   # 千问额外延迟下限（秒，2026-09-30 用户要求拉长到3分钟一次，防人工验证风控）
QW_DELAY_MAX = 210   # 千问额外延迟上限（秒）
DB_DELAY_MIN = 30    # 豆包额外延迟下限（秒，2026-09-29 两小时内两次人工验证，风控抖动期）
DB_DELAY_MAX = 45    # 豆包额外延迟上限（秒）
REST_EVERY = 10     # 每跑多少题休息一次
REST_MIN = 300      # 休息最短时间（秒）= 5分钟
REST_MAX = 600      # 休息最长时间（秒）= 10分钟
ASK_TIMEOUT = 300   # 单个模型单次提问超时（秒），5分钟

# ---------- 工具 ----------
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def call_model(model, question):
    """调用单个模型 /ask，返回 dict。失败抛异常。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{model['port']}/ask",
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": MODEL_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=ASK_TIMEOUT) as resp:
        data = json.loads(resp.read())
    return {
        "status": "ok",
        "answer": data.get("answer", ""),
        "search_queries": data.get("search_queries", []),
        "citations": data.get("citations", []),
        "asked_at": datetime.now().astimezone().isoformat(),
    }

def make_result_file(industry_file, prompt_text, verdict, depth, prompt_idx, results):
    """组装单个prompt的结果文件内容。"""
    return {
        "schema": "minsheng-geo-batch-result-v1",
        "industry_id": industry_file.get("industry_id"),
        "industry": industry_file.get("industry"),
        "cat_code": industry_file.get("cat_code"),
        "cat_name": industry_file.get("cat_name"),
        "div_name": industry_file.get("div_name"),
        "anchors": industry_file.get("anchors", []),
        "prompt_index": prompt_idx,
        "prompt_text": prompt_text,
        "verdict": verdict,
        "depth": depth,
        "collected_at": datetime.now().astimezone().isoformat(),
        "results": results,
    }

def is_rest_time():
    """每周休息日检查：周日不跑，让账号休息（与Dashboard一致）。"""
    return datetime.now().weekday() == 6  # 0=周一, 6=周日

# ---------- 核心跑批 ----------
async def ask_one_prompt(question):
    """同一问题并发发所有模型，等全部回来。返回 {model_id: result_dict}。"""
    async def _ask(m):
        try:
            if m["id"] == "deepseek":
                await asyncio.sleep(random.uniform(DS_DELAY_MIN, DS_DELAY_MAX))  # DeepSeek 专属降速
            elif m["id"] == "qianwen":
                await asyncio.sleep(random.uniform(QW_DELAY_MIN, QW_DELAY_MAX))  # 千问专属降速
            elif m["id"] == "doubao":
                await asyncio.sleep(random.uniform(DB_DELAY_MIN, DB_DELAY_MAX))  # 豆包专属降速
            # 硬超时兜底：run_in_executor 的线程不可中断，但主流程不等它——
            # wait_for 超时后立即返回 error 继续下一题，底层线程自行收尾，
            # 防止服务端流式慢发导致 urlopen 的 socket 超时永不触发、整题永久卡死。
            fut = asyncio.get_event_loop().run_in_executor(None, call_model, m, question)
            return m["id"], await asyncio.wait_for(fut, timeout=ASK_TIMEOUT)
        except asyncio.TimeoutError:
            return m["id"], {
                "status": "error",
                "error": f"硬超时 {ASK_TIMEOUT}s（服务端持续慢发未响应）",
                "answer": "",
                "search_queries": [],
                "citations": [],
                "asked_at": datetime.now().astimezone().isoformat(),
            }
        except Exception as e:
            return m["id"], {
                "status": "error",
                "error": str(e)[:200],
                "answer": "",
                "search_queries": [],
                "citations": [],
                "asked_at": datetime.now().astimezone().isoformat(),
            }
    models = active_models()
    # 千问专职补齐旧缺口期间（fix_runner 运行中）不参与新题；补齐完成后自动归队
    try:
        _fix = json.load(open(Path(__file__).parent / "fix_status.json", encoding="utf-8"))
        if not _fix.get("qianwen_done") and _fix.get("pending", 0) > 0:
            models = [m for m in models if m["id"] != "qianwen"]
    except Exception:
        models = [m for m in models if m["id"] != "qianwen"]
    results = await asyncio.gather(*[_ask(m) for m in models])
    return dict(results)

# ---------- 跑批状态（供驾驶舱读取） ----------
STATUS_FILE = Path(__file__).parent / "batch_status.json"
_batch_status = {
    "updated_at": "", "status": "starting",
    "industry_id": "", "industry_name": "", "industry_total": 0,
    "current_no": 0, "current_question": "",
    "total_asked": 0, "total_failed": 0, "total_skipped": 0,
    "models": {},
    "recent": [],  # 最近50题
}
_batch_model_stats = {}  # {model_id: {"ok": n, "err": n}}

def save_batch_status():
    """把当前跑批状态写入 batch_status.json（驾驶舱读取）。"""
    try:
        _batch_status["updated_at"] = datetime.now().astimezone().isoformat()
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(_batch_status, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _note_model_result(mid: str, ok: bool):
    st = _batch_model_stats.setdefault(mid, {"ok": 0, "err": 0})
    st["ok" if ok else "err"] += 1
    _batch_status["models"][mid] = {
        "last_ask": datetime.now().astimezone().isoformat(),
        "last_result": "ok" if ok else "error",
        "run_ok": st["ok"], "run_err": st["err"],
    }

def _note_question(no: int, text: str, failed: list, total: int, answered: list = None):
    _batch_status["recent"].insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "no": no, "question": text[:40],
        "failed": failed,
        "models": answered or [],  # 本题实际回答的模型（含成功/失败）
    })
    _batch_status["recent"] = _batch_status["recent"][:50]

def _set_industry(industry_id, name, total):
    _batch_status["industry_id"] = industry_id
    _batch_status["industry_name"] = name
    _batch_status["industry_total"] = total
    _batch_status["current_no"] = 0
    _batch_status["status"] = "running"

async def run_batch(industry_files, limit=None, only_industries=None):
    """遍历行业和prompt执行跑批。"""
    total_asked = 0
    total_skipped = 0
    total_failed = 0

    for i, (industry_id, ind_file) in enumerate(industry_files):
        if only_industries and industry_id not in only_industries:
            continue

        # 控制端暂停：停在当前行业，不提问不写文件
        while batch_paused():
            log("⏸️ 跑批已暂停（控制端），等待恢复...")
            await asyncio.sleep(10)

        # 行业目录：ID_行业名
        safe_name = str(ind_file.get("industry", str(industry_id))).replace("/", "_").replace("\\", "_")
        ind_dir = OUT_DIR / f"{industry_id}_{safe_name}"
        ind_dir.mkdir(parents=True, exist_ok=True)

        prompts = ind_file.get("prompts", [])
        log(f"=== 行业 {industry_id}/{len(industry_files)}: {ind_file.get('industry')}，{len(prompts)} 条prompt ===")
        _set_industry(str(industry_id), str(ind_file.get("industry", industry_id)), len(prompts))
        save_batch_status()

        for pidx, p in enumerate(prompts, 1):
            # 控制端暂停：停在当前题，等恢复后继续断点
            while batch_paused():
                log("⏸️ 跑批已暂停（控制端），等待恢复...")
                await asyncio.sleep(10)

            if limit and total_asked >= limit:
                log(f"达到测试上限 {limit} 条，停止。")
                return total_asked, total_skipped, total_failed

            text = p.get("text", "").strip()
            if not text:
                continue

            # 结果文件名：序号_前30字（清洗非法字符，防止/等字符创建子目录）
            safe_tail = re.sub(r'[\\/:*?"<>|\s]+', '_', text[:30]).strip('_') or f"q{pidx}"
            fn = f"{pidx:04d}_{safe_tail}.json"
            out_file = ind_dir / fn

            # 断点续跑：按序号匹配已落盘文件（不依赖文件名中的问题文本，prompt修改后仍可跳过）
            matched = list(ind_dir.glob(f"{pidx:04d}_*.json"))
            resume_file = matched[0] if matched else None
            # 已有新命名文件则优先，否则用旧文件名
            if out_file.exists():
                resume_file = out_file

            # 断点续跑：已存在且含完整结果则跳过；部分模型失败/缺失则只重跑失败/缺失的模型
            existing_results = {}
            need_retry = False
            active_ids = [m["id"] for m in active_models()]
            if resume_file is not None:
                try:
                    with open(resume_file, encoding="utf-8") as f:
                        old = json.load(f)
                    old_results = old.get("results") or {}
                    # 找出异常/失败的模型
                    for mid, r in old_results.items():
                        if r.get("status") != "ok":
                            need_retry = True
                        else:
                            existing_results[mid] = r
                    # 活跃模型缺失（如暂停期未参与）也要补
                    missing_active = [mid for mid in active_ids if mid not in old_results]
                    if missing_active:
                        need_retry = True
                    if old_results and not need_retry:
                        total_skipped += 1
                        continue
                except Exception:
                    pass

            if need_retry:
                # 双轨并行：缺失/失败的模型缺口由独立补齐进程(fix_runner.py)处理，
                # 主循环不原地等待，直接跳过本题继续推进新题，其他模型不闲置
                log(f"  ⏭️ 缺口题跳过（缺失 {[mid for mid, r in (old.get('results') or {}).items() if r.get('status') != 'ok'] + [mid for mid in active_ids if mid not in (old.get('results') or {})]}，由补齐进程处理）")
                continue
            log(f"[{pidx}/{len(prompts)}] 提问: {text[:50]}...")
            _batch_status["current_no"] = pidx
            _batch_status["current_question"] = text[:40]
            save_batch_status()
            results = await ask_one_prompt(text)

            # 统计失败
            # 统计各模型结果（供状态文件）——只统计本轮实际调用的模型，
            # 复用历史结果（existing_results）的模型不刷新 last_ask/last_result，避免误报"刚刚回答"
            if need_retry:
                for mid, r in retry_results.items():
                    _note_model_result(mid, r.get("status") == "ok")
            else:
                for mid, r in results.items():
                    _note_model_result(mid, r.get("status") == "ok")
            failed_models = [mid for mid, r in results.items() if r.get("status") != "ok"]
            if failed_models:
                total_failed += 1
                log(f"  ⚠️ 失败模型: {failed_models}，标记error继续（下次运行将自动重试）")

            result = make_result_file(ind_file, text, p.get("verdict"), p.get("depth"), pidx, results)

            # 原子写：先写临时文件再改名，避免半截文件
            tmp_file = out_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, out_file)

            total_asked += 1
            log(f"  ✅ 完成，引用数: {sum(len(r.get('citations',[])) for r in results.values())}")
            _batch_status["total_asked"] = total_asked
            _batch_status["total_failed"] = total_failed
            _batch_status["total_skipped"] = total_skipped
            _note_question(pidx, text, failed_models, len(prompts),
                          answered=[mid for mid in (retry_results if need_retry else results)])
            _batch_status["status"] = "resting" if total_asked % REST_EVERY == 0 else "running"
            save_batch_status()

            # 防风控节奏
            if total_asked % REST_EVERY == 0:
                rest = random.randint(REST_MIN, REST_MAX)
                log(f"🛌 已跑 {total_asked} 题，休息 {rest//60} 分钟...")
                _batch_status["status"] = "resting"
                save_batch_status()
                await asyncio.sleep(rest)
                # 休息结束立即恢复运行状态，避免休息后第一题期间误显示"休息中"
                _batch_status["status"] = "running"
                save_batch_status()
            else:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                log(f"⏳ 等待 {delay:.1f} 秒...")
                await asyncio.sleep(delay)

    return total_asked, total_skipped, total_failed

def main():
    parser = argparse.ArgumentParser(description="民生行业大模型跑批引擎")
    parser.add_argument("--limit", type=int, default=None, help="只跑前N条（小批量试跑）")
    parser.add_argument("--industry", type=int, nargs="*", default=None, help="只跑指定行业ID，可多个")
    parser.add_argument("--no-rest-check", action="store_true", help="跳过周日休息检查")
    args = parser.parse_args()

    if not SRC_DIR.exists():
        log(f"❌ 数据目录不存在: {SRC_DIR}")
        sys.exit(1)
    if is_rest_time() and not args.no_rest_check:
        log("今天是周日休息日，系统休息，不跑批。可用 --no-rest-check 强制跑。")
        sys.exit(0)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 加载行业文件
    industry_files = []
    for fn in sorted(SRC_DIR.glob("*.json")):
        if fn.name == "manifest.json":
            continue
        try:
            with open(fn, encoding="utf-8") as f:
                ind = json.load(f)
            industry_files.append((ind.get("industry_id"), ind))
        except Exception as e:
            log(f"⚠️ 跳过无法解析的文件 {fn.name}: {e}")

    log(f"加载完成：{len(industry_files)} 个行业文件")

    if args.industry:
        industry_files = [(iid, f) for iid, f in industry_files if iid in args.industry]
        log(f"只跑指定行业: {args.industry}，共 {len(industry_files)} 个")

    total_asked, total_skipped, total_failed = asyncio.run(
        run_batch(industry_files, limit=args.limit, only_industries=args.industry)
    )
    log(f"🏁 跑批结束：本轮提问 {total_asked}，断点跳过 {total_skipped}，失败 {total_failed}")

if __name__ == "__main__":
    main()
