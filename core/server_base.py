"""
FastAPI 应用工厂：所有模型服务共享的 HTTP 接口骨架。
接收一个 BrowserBase 实例，自动提供 /ask、/batch_ask、/ 健康检查、日志落盘。
"""
import os
import asyncio
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.browser_base import BrowserBase, LOG_DIR

# API Key 鉴权：强制从环境变量读，未配置直接拒绝启动
API_KEY = os.getenv("LAYA_API_KEY")
if not API_KEY:
    raise RuntimeError("必须设置环境变量 LAYA_API_KEY")


def verify_api_key(request: Request):
    """强制 API Key 校验，不允许未鉴权访问"""
    api_key = request.headers.get("X-API-Key", "")
    if api_key != API_KEY:
        raise HTTPException(status_code=401, detail="无效的 API Key")


class AskRequest(BaseModel):
    question: str


class BatchAskRequest(BaseModel):
    questions: list[str]


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

    lock = asyncio.Lock()
    browser_ref = {"b": None}  # startup 后填入

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
        return {"status": "ok", "service": service_name}

    @app.post("/ask", dependencies=[Depends(verify_api_key)])
    async def ask(req: AskRequest):
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="问题不能为空")
        async with lock:
            try:
                result = await browser.ask(req.question)
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
                raise HTTPException(status_code=502, detail=f"{service_name} 请求失败: {str(e)}")

        result = {
            "question": req.question,
            "answer": result["answer"],
            "citations": result["citations"],
            "search_queries": result.get("search_queries", []),
        }
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

        async with lock:
            for q in req.questions:
                q = q.strip()
                if not q:
                    failed += 1
                    record = {"question": q, "error": "问题为空"}
                    save_log(record, service_name)
                    results.append(record)
                    continue
                try:
                    r = await browser.ask(q)
                    record = {
                        "question": q,
                        "answer": r["answer"],
                        "citations": r["citations"],
                        "search_queries": r.get("search_queries", []),
                    }
                    save_log(record, service_name)
                    results.append(record)
                    succeeded += 1
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

        return {
            "total": len(req.questions),
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        }

    return app
