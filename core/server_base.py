"""
FastAPI 应用工厂：所有模型服务共享的 HTTP 接口骨架。
接收一个 BrowserBase 实例，自动提供 /ask、/batch_ask、/ 健康检查、日志落盘。
"""
import os
import asyncio
import json
from datetime import datetime
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.browser_base import BrowserBase, LOG_DIR, CaptchaDetected

# API Key 鉴权：强制从环境变量读，未配置直接拒绝启动
API_KEY = os.getenv("LAYA_API_KEY")
if not API_KEY:
    raise RuntimeError("必须设置环境变量 LAYA_API_KEY")


# 连续失败计数：达到阈值触发页面重置（解决"页面正常但持续失败"）
_fail_counts = {}
_FAIL_RECOVER_THRESHOLD = 3


def _bump_fail(service_name: str) -> int:
    _fail_counts[service_name] = _fail_counts.get(service_name, 0) + 1
    return _fail_counts[service_name]


def _reset_fail(service_name: str):
    _fail_counts[service_name] = 0


def _auto_pause(service_name: str):
    """人工验证自动暂停：写入 paused_models.json（与 runner/dashboard 共用，按 model_id）。"""
    pf = Path(__file__).parent.parent / "paused_models.json"
    try:
        data = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else []
        if service_name not in data:
            data.append(service_name)
            pf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"⚠️ [{service_name}] 人工验证，已自动暂停模型", flush=True)
    except Exception as e:
        print(f"[auto_pause] 失败: {e}", flush=True)


def _play_alert():
    """播放报警：响铃3次 + 语音播报。"""
    import subprocess
    try:
        for _ in range(3):
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
        subprocess.run(["say", "警告：模型出现人工验证，请检查处理"], check=False)
    except Exception as e:
        print(f"[报警] 播放失败: {e}", flush=True)


class PriorityLock:
    """按优先级 + 到达顺序获取的锁。

    - priority 高的请求先获得锁（上游实时任务 priority=10，农场批处理 priority=0）。
    - 同优先级按先到先得（FIFO）。
    - 不抢占正在执行的任务，只控制等待队列顺序——浏览器单实例同一时刻仍只处理一个 ask。
    """

    def __init__(self):
        self._locked = False
        self._waiters = []  # [(priority, seq, future)]
        self._seq = 0

    async def acquire(self, priority: int = 0):
        if not self._locked:
            self._locked = True
            return
        seq = self._seq
        self._seq += 1
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._waiters.append((priority, seq, fut))
        self._waiters.sort(key=lambda x: (-x[0], x[1]))
        try:
            await fut
        except asyncio.CancelledError:
            try:
                self._waiters.remove((priority, seq, fut))
            except ValueError:
                pass
            raise

    def release(self):
        if self._waiters:
            _, _, fut = self._waiters.pop(0)
            if not fut.done():
                fut.set_result(None)
        else:
            self._locked = False


def verify_api_key(request: Request):
    """强制 API Key 校验，不允许未鉴权访问"""
    api_key = request.headers.get("X-API-Key", "")
    if api_key != API_KEY:
        raise HTTPException(status_code=401, detail="无效的 API Key")


class AskRequest(BaseModel):
    question: str
    priority: int = 0  # 0=农场批处理；10=上游实时任务（优先插队）


class BatchAskRequest(BaseModel):
    questions: list[str]
    priority: int = 0


def save_log(record: dict, service_name: str):
    """保存一条请求记录到按日期分的 JSONL 文件（按服务名分文件，单文件超过100MB自动分割）"""
    today = datetime.now().strftime("%Y-%m-%d")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    # 找当前日期的最大序号，单文件超过100MB就分下一个
    MAX_SIZE = 100 * 1024 * 1024  # 100MB
    seq = 1
    while True:
        if seq == 1:
            log_file = LOG_DIR / f"{today}.jsonl"
        else:
            log_file = LOG_DIR / f"{today}_{seq}.jsonl"
        if not log_file.exists() or log_file.stat().st_size < MAX_SIZE:
            break
        seq += 1
    
    record["timestamp"] = datetime.now().isoformat()
    record["service"] = service_name
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def create_app(browser: BrowserBase, service_name: str) -> FastAPI:
    """根据一个浏览器实例创建完整的 FastAPI 应用。"""
    app = FastAPI(title=service_name)

    # 允许跨域（Dashboard 9000 端口调用）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 本地工具，允许所有来源
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    lock = PriorityLock()
    browser_ref = {"b": None}  # startup 后填入
    runtime_state = {"last_success_at": None, "busy": False}

    @app.on_event("startup")
    async def startup():
        browser_ref["b"] = browser
        await browser.start()
        if not await browser.is_logged_in():
            print("=" * 50)
            print(f"[警告] {service_name} 未登录！请在 Chrome 窗口中手动登录")
            print("服务已启动，但 /ask 会返回 401")
            print("=" * 50, flush=True)
        else:
            print(f"[启动] {service_name} 已登录，服务就绪", flush=True)

    @app.on_event("shutdown")
    async def shutdown():
        """服务停止时优雅关闭浏览器，避免 Chrome 写崩溃标记导致下次弹"恢复页面"提示"""
        print(f"[关闭] {service_name} 优雅关闭浏览器...", flush=True)
        b = browser_ref["b"]
        if b:
            try:
                await b.close()
            except Exception as e:
                print(f"[关闭] {service_name} 浏览器关闭异常: {e}", flush=True)

    @app.get("/")
    async def health():
        b = browser_ref["b"]
        logged_in = False
        captcha = None
        if b and not runtime_state["busy"]:
            try:
                logged_in = await asyncio.wait_for(b.is_logged_in(), timeout=3)
                captcha = await asyncio.wait_for(b._check_captcha(strict=True), timeout=5)
            except:
                pass
        return {
            "status": "ok",
            "service": service_name,
            "browser_alive": True,
            "logged_in": logged_in,
            "captcha_detected": captcha,
            "busy": runtime_state["busy"],
            "last_success_at": runtime_state["last_success_at"],
        }

    @app.post("/ask", dependencies=[Depends(verify_api_key)])
    async def ask(req: AskRequest):
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="问题不能为空")
        await lock.acquire(priority=req.priority)
        try:
            runtime_state["busy"] = True
            try:
                result = await asyncio.wait_for(browser.ask(req.question), timeout=180)
            except asyncio.TimeoutError:
                save_log({
                    "question": req.question,
                    "error": "请求超时(180s)",
                    "answer": "",
                    "citations": [],
                    "search_queries": [],
                }, service_name)
                # 连续失败恢复：刷新页面重置状态
                if _bump_fail(service_name) >= _FAIL_RECOVER_THRESHOLD:
                    print(f"[{service_name}] 连续失败{_fail_counts[service_name]}次，重置页面", flush=True)
                    await browser.recover_page()
                    _reset_fail(service_name)
                raise HTTPException(status_code=502, detail=f"{service_name} 请求超时")
            except CaptchaDetected:
                # 人工验证：暂停模型 + 报警，让用户处理后恢复
                _auto_pause(service_name)
                _play_alert()
                save_log({
                    "question": req.question,
                    "error": "人工验证，已自动暂停模型",
                    "answer": "",
                    "citations": [],
                    "search_queries": [],
                }, service_name)
                raise HTTPException(status_code=503,
                                     detail=f"{service_name} 人工验证，已暂停模型；请处理后恢复")
            except HTTPException as e:
                # 失败也记录日志
                save_log({
                    "question": req.question,
                    "error": e.detail,
                    "answer": "",
                    "citations": [],
                    "search_queries": [],
                }, service_name)
                raise
            except Exception as e:
                # 失败也记录日志
                save_log({
                    "question": req.question,
                    "error": str(e),
                    "answer": "",
                    "citations": [],
                    "search_queries": [],
                }, service_name)
                # 连续失败恢复：刷新页面重置状态
                if _bump_fail(service_name) >= _FAIL_RECOVER_THRESHOLD:
                    print(f"[{service_name}] 连续失败{_fail_counts[service_name]}次，重置页面", flush=True)
                    await browser.recover_page()
                    _reset_fail(service_name)
                raise HTTPException(status_code=502, detail=f"{service_name} 请求失败: {str(e)}")
        finally:
            runtime_state["busy"] = False
            lock.release()
            _reset_fail(service_name)

        result = {
            "question": req.question,
            "answer": result["answer"],
            "citations": result["citations"],
            "search_queries": result.get("search_queries", []),
        }
        runtime_state["last_success_at"] = datetime.now().astimezone().isoformat()
        save_log(result, service_name)
        return result

    @app.post("/batch_ask", dependencies=[Depends(verify_api_key)])
    async def batch_ask(req: BatchAskRequest):
        if not req.questions:
            raise HTTPException(status_code=400, detail="questions 不能为空")
        if len(req.questions) > 100:
            raise HTTPException(status_code=400, detail="单次最多 100 个问题")

        results = []
        succeeded = failed = 0

        await lock.acquire(priority=req.priority)
        try:
            for q in req.questions:
                q = q.strip()
                if not q:
                    failed += 1
                    record = {"question": q, "error": "问题为空"}
                    save_log(record, service_name)
                    results.append(record)
                    continue
                try:
                    r = await asyncio.wait_for(browser.ask(q), timeout=180)
                    record = {
                        "question": q,
                        "answer": r["answer"],
                        "citations": r["citations"],
                        "search_queries": r.get("search_queries", []),
                    }
                    save_log(record, service_name)
                    results.append(record)
                    succeeded += 1
                    runtime_state["last_success_at"] = datetime.now().astimezone().isoformat()
                except HTTPException as e:
                    failed += 1
                    record = {"question": q, "error": e.detail}
                    save_log(record, service_name)
                    results.append(record)
                except Exception as e:
                    failed += 1
                    record = {"question": q, "error": f"{service_name} 请求失败: {e}"}
                    save_log(record, service_name)
                    results.append(record)
        finally:
            runtime_state["busy"] = False
            lock.release()

        return {
            "total": len(req.questions),
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        }

    return app
