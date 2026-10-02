"""
ask_service.py — 上游问答服务模式（形态2：服务化流水线内核）

与农场模式共存：
- 共用 5 个模型服务（8000-8004）。模型服务已升级为优先级锁：
  本服务提交上游任务 priority=10（插队优先），农场跑批 priority=0。
- 每模型一个 asyncio 工作队列 + worker：多任务交错消化，模型不再空转。

API：
  POST /v1/ask {question, models?, priority?, callback_url?, metadata?} -> {request_id, status}
  GET  /v1/tasks/{task_id}   任务详情与结果（可轮询）
  POST /v1/tasks/{task_id}/cancel  取消未完成任务
  GET  /v1/health  服务健康
  GET  /v1/models  5 个模型实时状态

任务状态机：
  queued -> running -> completed | partial | failed | cancelled | skipped
results[model_id] = {status: ok|error|skipped, answer, citations, search_queries, error, finished_at}

鉴权：X-API-Key: <SERVICE_API_KEY>（环境变量，默认 laya-service-key）
回调：任务终态后 POST callback_url {request_id, question, status, results, completed_at}，失败重试 2 次
"""
import os
import sys
import json
import uuid
import random
import asyncio
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
TASKS_DIR = BASE_DIR / "service_tasks"
TASKS_DIR.mkdir(exist_ok=True)
PAUSED_FILE = BASE_DIR / "paused_models.json"

SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "laya-service-key")
MODEL_API_KEY = os.getenv("LAYA_API_KEY", "laya-local-model-key")
UPSTREAM_PRIORITY = 10      # 上游任务在模型优先级锁中的权重
ASK_TIMEOUT = 260           # 单模型提问硬超时，与模型服务一致（> browser.ask 内部240s上限，避免外层取消打断playwright）

# 防风控节奏（与农场模式对齐）：每个模型两次提问之间的最小/最大间隔（秒）。
# 上游任务同样继承此节奏，避免连续快速提问触发平台风控。
MODEL_DELAY = {
    "deepseek": (150, 210),   # 风控最敏感，2.5-3.5 分钟
    "qianwen": (180, 210),    # 3 分钟
    "doubao": (30, 45),       # 30-45s
    "wenxin": (15, 25),       # 通用节奏
    "yuanbao": (15, 25),      # 通用节奏
}

MODELS_ALL = [
    {"id": "deepseek", "name": "DeepSeek", "port": 8000},
    {"id": "qianwen", "name": "千问", "port": 8001},
    {"id": "doubao", "name": "豆包", "port": 8002},
    {"id": "wenxin", "name": "文心一言", "port": 8003},
    {"id": "yuanbao", "name": "腾讯元宝", "port": 8004},
]
MODEL_BY_ID = {m["id"]: m for m in MODELS_ALL}


# ---------- 工具 ----------
def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_paused() -> set:
    try:
        if PAUSED_FILE.exists():
            return set(json.loads(PAUSED_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def save_task(task: dict):
    fp = TASKS_DIR / f"{task['request_id']}.json"
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)


def load_task(task_id: str) -> Optional[dict]:
    fp = TASKS_DIR / f"{task_id}.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------- 模型调用（priority=10 上游优先） ----------
async def call_model(model: dict, question: str) -> dict:
    """调用模型 /ask，返回结果 dict。失败抛 RuntimeError。"""
    payload = json.dumps({"question": question, "priority": UPSTREAM_PRIORITY}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{model['port']}/ask",
        data=payload,
        headers={"Content-Type": "application/json", "X-API-Key": MODEL_API_KEY},
        method="POST",
    )

    def _do():
        with urllib.request.urlopen(req, timeout=ASK_TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _do)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", str(e))
        except Exception:
            detail = str(e)
        raise RuntimeError(f"{model['id']}: HTTP {e.code} {detail}")
    except asyncio.TimeoutError:
        raise RuntimeError(f"{model['id']}: 超时({ASK_TIMEOUT}s)")
    except Exception as e:
        raise RuntimeError(f"{model['id']}: {e}")


# ---------- 请求模型 ----------
class AskIn(BaseModel):
    question: str
    models: Optional[list[str]] = None        # 缺省 = 全部可用模型
    priority: int = UPSTREAM_PRIORITY
    callback_url: Optional[str] = None
    metadata: Optional[dict] = None


# ---------- 任务管理器 ----------
class TaskManager:
    def __init__(self):
        self.queues = {m["id"]: asyncio.Queue() for m in MODELS_ALL}
        self.tasks: dict[str, dict] = {}
        self.workers: list[asyncio.Task] = []

    # ---- 启动恢复：queued/running 的任务重新入队未完成模型 ----
    async def recover(self):
        for fp in sorted(TASKS_DIR.glob("*.json")):
            try:
                t = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if t.get("status") not in ("queued", "running"):
                continue
            t["status"] = "queued"
            t["started_at"] = None
            self.tasks[t["request_id"]] = t
            reenqueued = False
            for mid in t.get("models", []):
                r = t.get("results", {}).get(mid) or {}
                if r.get("status") not in ("ok", "error", "skipped"):
                    await self.queues[mid].put(t["request_id"])
                    reenqueued = True
            save_task(t)
            if reenqueued:
                log(f"恢复任务 {t['request_id']}（{t['question'][:24]}...）")

    async def start_workers(self):
        for mid in self.queues:
            self.workers.append(asyncio.create_task(self._worker(mid)))

    # ---- 提交任务 ----
    async def submit(self, ask: AskIn) -> dict:
        q = ask.question.strip()
        if not q:
            raise HTTPException(status_code=400, detail="问题不能为空")
        models = ask.models or [m["id"] for m in MODELS_ALL]
        models = [m for m in models if m in MODEL_BY_ID]
        if not models:
            raise HTTPException(status_code=400, detail="没有可用模型")

        paused = load_paused()
        task = {
            "request_id": uuid.uuid4().hex[:16],
            "question": q,
            "models": models,
            "priority": ask.priority,
            "callback_url": ask.callback_url,
            "metadata": ask.metadata or {},
            "status": "queued",
            "results": {},
            "created_at": now_iso(),
            "started_at": None,
            "completed_at": None,
        }
        # 暂停中的模型直接标 skipped（如 DeepSeek 封禁中），不占用队列
        for mid in models:
            if mid in paused:
                task["results"][mid] = {
                    "status": "skipped", "error": "模型暂停中",
                    "finished_at": now_iso(),
                }
        save_task(task)
        self.tasks[task["request_id"]] = task
        for mid in models:
            if mid not in task["results"]:
                await self.queues[mid].put(task["request_id"])
        log(f"新任务 {task['request_id']} models={models} 状态={task['status']}")
        return task

    # ---- 每模型 worker：交错消化任务 ----
    async def _worker(self, mid: str):
        q = self.queues[mid]
        model = MODEL_BY_ID[mid]
        while True:
            rid = await q.get()
            try:
                task = self.tasks.get(rid)
                if not task:
                    continue
                if task["status"] == "cancelled":
                    continue
                # 入队后模型才被暂停：标 skipped
                if mid in load_paused():
                    task["results"][mid] = {
                        "status": "skipped", "error": "模型暂停中",
                        "finished_at": now_iso(),
                    }
                    save_task(task)
                    await self._maybe_finalize(task)
                    continue
                task["status"] = "running"
                task["started_at"] = task["started_at"] or now_iso()
                save_task(task)
                # 执行，失败自动重试 1 次
                err = None
                for attempt in range(2):
                    try:
                        r = await asyncio.wait_for(
                            call_model(model, task["question"]), timeout=ASK_TIMEOUT + 15)
                        task["results"][mid] = {
                            "status": "ok",
                            "answer": r.get("answer", ""),
                            "citations": r.get("citations", []),
                            "search_queries": r.get("search_queries", []),
                            "finished_at": now_iso(),
                        }
                        err = None
                        break
                    except asyncio.TimeoutError:
                        err = f"{mid}: 超时({ASK_TIMEOUT}s)"
                    except Exception as e:
                        err = str(e)
                        await asyncio.sleep(3 * (attempt + 1))
                if err:
                    task["results"][mid] = {
                        "status": "error", "error": err, "finished_at": now_iso(),
                    }
                    log(f"任务 {rid} {mid} 失败: {err}")
                save_task(task)
                await self._maybe_finalize(task)
                # 防风控：模型专属提问间隔（与农场模式对齐，成功失败都生效，避免连续快速提问触发风控）
                lo, hi = MODEL_DELAY.get(mid, (15, 25))
                await asyncio.sleep(random.uniform(lo, hi))
            finally:
                q.task_done()

    # ---- 终态判定 + 回调 ----
    async def _maybe_finalize(self, task: dict):
        if task["status"] in ("cancelled", "completed", "failed", "partial"):
            return
        if not all(mid in task["results"] for mid in task["models"]):
            return
        n_ok = sum(1 for r in task["results"].values() if r.get("status") == "ok")
        n_total = len(task["models"])
        if n_ok == n_total:
            task["status"] = "completed"
        elif n_ok > 0:
            task["status"] = "partial"
        else:
            task["status"] = "failed"
        task["completed_at"] = now_iso()
        save_task(task)
        log(f"任务 {task['request_id']} 终态: {task['status']}（{n_ok}/{n_total} ok）")
        if task.get("callback_url"):
            asyncio.create_task(self._callback(task))

    async def _callback(self, task: dict):
        payload = {
            "request_id": task["request_id"],
            "question": task["question"],
            "status": task["status"],
            "results": task["results"],
            "metadata": task.get("metadata", {}),
            "completed_at": task["completed_at"],
        }
        data = json.dumps(payload, ensure_ascii=False).encode()

        def _post():
            req = urllib.request.Request(
                task["callback_url"], data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        for attempt in range(3):
            try:
                loop = asyncio.get_event_loop()
                status = await loop.run_in_executor(None, _post)
                log(f"回调成功 {task['request_id']} -> {status}")
                return
            except Exception as e:
                log(f"回调失败(第{attempt + 1}/3) {task['request_id']}: {e}")
                await asyncio.sleep(3 * (attempt + 1))

    # ---- 取消 ----
    async def cancel(self, task_id: str) -> dict:
        task = self.tasks.get(task_id) or load_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task["status"] in ("completed", "failed", "partial", "cancelled"):
            return task
        task["status"] = "cancelled"
        task["completed_at"] = now_iso()
        save_task(task)
        log(f"任务 {task_id} 已取消")
        return task


# ---------- FastAPI ----------
manager: TaskManager = None  # lifespan 中延迟初始化（需在运行中的事件循环里创建 Queue）


def verify_service_key(request: Request):
    key = request.headers.get("X-API-Key", "")
    if key != SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="无效的 API Key")


@asynccontextmanager
async def lifespan(app):
    """在运行中的事件循环里初始化队列与 worker（避免模块级 Queue 绑定错误 loop）。"""
    global manager
    manager = TaskManager()
    await manager.recover()
    await manager.start_workers()
    log(f"服务就绪，模型: {[m['id'] for m in MODELS_ALL]}")
    try:
        yield
    finally:
        for w in manager.workers:
            w.cancel()


app = FastAPI(title="Laya 上游问答服务", lifespan=lifespan,
              docs_url="/docs", openapi_url="/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/v1/health")
async def health():
    return {"status": "ok", "service": "ask_service", "queued_tasks": len([
        t for t in manager.tasks.values() if t["status"] == "queued"])}


@app.post("/v1/ask", dependencies=[Depends(verify_service_key)])
async def ask(req: AskIn):
    task = await manager.submit(req)
    return {
        "request_id": task["request_id"],
        "status": task["status"],
        "models": task["models"],
        "created_at": task["created_at"],
    }


@app.get("/v1/tasks/{task_id}", dependencies=[Depends(verify_service_key)])
async def get_task(task_id: str):
    task = manager.tasks.get(task_id) or load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.post("/v1/tasks/{task_id}/cancel", dependencies=[Depends(verify_service_key)])
async def cancel_task(task_id: str):
    return await manager.cancel(task_id)


@app.get("/v1/models", dependencies=[Depends(verify_service_key)])
async def models_status():
    paused = load_paused()
    out = []
    for m in MODELS_ALL:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{m['port']}/",
                headers={"X-API-Key": MODEL_API_KEY})
            with urllib.request.urlopen(req, timeout=8) as resp:
                st = json.loads(resp.read().decode())
            out.append({
                "id": m["id"], "name": m["name"],
                "paused": m["id"] in paused,
                "logged_in": st.get("logged_in"),
                "busy": st.get("busy"),
                "captcha_detected": st.get("captcha_detected"),
                "last_success_at": st.get("last_success_at"),
            })
        except Exception as e:
            out.append({
                "id": m["id"], "name": m["name"],
                "paused": m["id"] in paused,
                "error": str(e),
            })
    return {"models": out}


if __name__ == "__main__":
    port = int(os.getenv("ASK_SERVICE_PORT", "9100"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
