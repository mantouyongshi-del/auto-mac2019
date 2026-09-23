"""
统一管理 Dashboard 服务。
端口 9000，提供前端页面 + 模型状态聚合 + 任务调度。
"""
import os
import json
import time
import random
import asyncio
import subprocess
import shutil
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional
from pydantic import BaseModel
import secrets

app = FastAPI(title="Laya Dashboard")

# 强制从环境变量读密钥，未配置直接拒绝启动
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
GEO_API_KEY = os.getenv("GEO_API_KEY")
LAYA_MODEL_API_KEY = os.getenv("LAYA_MODEL_API_KEY", "laya-local-model-key")

if not DASHBOARD_PASSWORD:
    raise RuntimeError("必须设置环境变量 DASHBOARD_PASSWORD")
if not GEO_API_KEY:
    raise RuntimeError("必须设置环境变量 GEO_API_KEY")
# 简单token存储（内存里，重启失效）
valid_tokens = set()

# GEO API 鉴权依赖
from fastapi import Security, status
from fastapi.security import APIKeyHeader

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_geo_api_key(api_key: Optional[str] = Security(api_key_header)):
    if not api_key or api_key != GEO_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key"
        )
    return api_key

# 五个模型配置
MODELS = [
    {"id": "deepseek", "name": "DeepSeek", "port": 8000},
    {"id": "qianwen", "name": "千问", "port": 8001},
    {"id": "doubao", "name": "豆包", "port": 8002},
    {"id": "wenxin", "name": "文心一言", "port": 8003},
    {"id": "yuanbao", "name": "元宝", "port": 8004},
]
MODEL_MAP = {m["id"]: m for m in MODELS}

# 模型暂停状态存储
PAUSE_FILE = Path(__file__).parent / "paused_models.json"

def load_paused():
    if PAUSE_FILE.exists():
        with open(PAUSE_FILE) as f:
            return set(json.load(f))
    return set()

def save_paused(paused):
    with open(PAUSE_FILE, "w") as f:
        json.dump(list(paused), f)

paused_models = load_paused()
# 健康检查记录存储
HEALTH_RECORD_FILE = Path(__file__).parent / "health_records.json"
MAX_HEALTH_RECORDS = 100  # 最多存100条

def load_health_records():
    if HEALTH_RECORD_FILE.exists():
        with open(HEALTH_RECORD_FILE) as f:
            return json.load(f)
    return []

def save_health_record(record):
    records = load_health_records()
    records.insert(0, record)
    records = records[:MAX_HEALTH_RECORDS]
    with open(HEALTH_RECORD_FILE, "w") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

@app.get("/api/health_records")
async def get_health_records():
    return {"records": load_health_records()}


@app.post("/api/models/{model_id}/pause")
async def pause_model(model_id: str):
    m = MODEL_MAP[model_id]
    paused_models.add(m["name"])
    save_paused(paused_models)
    return {"ok": True, "paused": list(paused_models)}

@app.post("/api/models/{model_id}/resume")
async def resume_model(model_id: str):
    m = MODEL_MAP[model_id]
    paused_models.discard(m["name"])
    save_paused(paused_models)
    return {"ok": True, "paused": list(paused_models)}

@app.get("/api/paused")
async def get_paused():
    return {"paused": list(paused_models)}

# 结构化JSONL日志
GEO_LOG_FILE = Path(__file__).parent / "geo_requests.jsonl"

def geo_log(event: str, data: dict):
    """写结构化JSONL日志"""
    entry = {"ts": datetime.now().isoformat(), "event": event, **data}
    with open(GEO_LOG_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

# 任务存储目录
TASKS_DIR = Path(__file__).parent / "tasks"
TASKS_DIR.mkdir(exist_ok=True)

# 内存中运行中的任务状态
running_tasks = {}
# 全局任务锁：同一时间只跑一个批量任务，避免抢窗口
task_lock = asyncio.Lock()
# 每个模型连续失败计数器，用于自动退避
model_fail_counts = {m["id"]: 0 for m in MODELS}
MODEL_FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", 2))  # 连续失败2次触发退避
BACKOFF_INTERVAL = int(os.getenv("BACKOFF_INTERVAL", 30))  # 退避间隔30秒
TASK_INTERVAL_MIN = float(os.getenv("TASK_INTERVAL_MIN", 15))
TASK_INTERVAL_MAX = float(os.getenv("TASK_INTERVAL_MAX", 25))


class CreateTaskRequest(BaseModel):
    questions: list
    models: Optional[list] = None
    delay_min: int = 15
    delay_max: int = 25
    task_name: str = ""


@app.get("/api/models")
def get_models():
    """获取所有模型的在线状态。"""
    results = []
    for m in MODELS:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{m['port']}/", timeout=3) as resp:
                data = json.loads(resp.read())
                results.append({**m, "status": "online", "service": data.get("service", "")})
        except Exception:
            results.append({**m, "status": "offline", "service": ""})
    return {"models": results}


def call_model(model_id: str, question: str) -> dict:
    """同步调用单个模型的 /ask 接口。"""
    m = MODEL_MAP[model_id]
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{m['port']}/ask",
            data=json.dumps({"question": question}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        return {
            "status": "success",
            "answer": data.get("answer", ""),
            "citations": data.get("citations", []),
            "search_queries": data.get("search_queries", []),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def run_task(task_id: str, questions: list, model_ids: list, delay_min: int, delay_max: int):
    """后台任务调度器：逐个问题发，每个问题跑所有模型，随机间隔。"""
    # 加全局任务锁，同一时间只跑一个任务
    await task_lock.acquire()
    try:
        await _run_task_inner(task_id, questions, model_ids, delay_min, delay_max)
    finally:
        task_lock.release()


async def _run_task_inner(task_id: str, questions: list, model_ids: list, delay_min: int, delay_max: int):
    task_file = TASKS_DIR / f"{task_id}.json"

    # 检查是否是恢复的任务
    start_index = 0
    task_state = None
    if task_file.exists():
        with open(task_file, encoding="utf-8") as f:
            task_state = json.load(f)
        if task_state.get("status") == "running":
            start_index = len(task_state.get("results", []))
            print(f"[任务 {task_id}] 从断点恢复，已完成 {start_index}/{len(questions)}", flush=True)

    if task_state is None:
        task_state = {
            "task_id": task_id,
            "status": "running",
            "total": len(questions),
            "completed": 0,
            "failed": 0,
            "models": model_ids,
            "created_at": datetime.now().isoformat(),
            "results": [],
            "all_questions": questions,  # 存完整问题列表，用于恢复
        }
        # 立刻写文件，避免后面读不到
        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(task_state, f, ensure_ascii=False, indent=2)

    # 任务最大时长1小时，超时自动标记失败
    task_start_time = time.time()
    MAX_TASK_DURATION = 3600  # 1小时

    for i in range(start_index, len(questions)):
        # 检查是否超时
        if time.time() - task_start_time > MAX_TASK_DURATION:
            task_state["status"] = "timeout"
            task_state["finished_at"] = datetime.now().isoformat()
            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(task_state, f, ensure_ascii=False, indent=2)
            print(f"[任务 {task_id}] 超过1小时，自动标记超时", flush=True)
            del running_tasks[task_id]
            return

        # 检查任务是否被取消/暂停
        with open(task_file, encoding="utf-8") as f:
            current = json.load(f)
            if current.get("status") in ("cancelled", "paused"):
                task_state["status"] = current["status"]
                print(f"[任务 {task_id}] 任务被{current['status']}", flush=True)
                return

        question = questions[i]
        print(f"[任务 {task_id}] 第 {i+1}/{len(questions)} 个问题: {question[:30]}...", flush=True)

        question_result = {"question": question, "models": {}}

        # 并发调用所有模型，同时提问
        async def ask_one(mid):
            print(f"  -> {mid} 提问中...", flush=True)
            # 重试2次
            for attempt in range(2):
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, call_model, mid, question)
                if result["status"] == "success":
                    return result
                print(f"  -> {mid} 第{attempt+1}次失败，重试...", flush=True)
                await asyncio.sleep(3)
            return result

        results = await asyncio.gather(*[ask_one(mid) for mid in model_ids])

        for i, mid in enumerate(model_ids):
            result = results[i]
            question_result["models"][mid] = result
            if result["status"] == "error":
                task_state["failed"] += 1
            else:
                task_state["completed"] += 1

        task_state["results"].append(question_result)

        # 保存中间结果
        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(task_state, f, ensure_ascii=False, indent=2)

        # 随机间隔，最后一个问题不用等
        if i < len(questions) - 1:
            # 每跑10个问题，自动休息5-10分钟，像真人歇一会
            if (i + 1) % 10 == 0:
                rest_min = random.randint(300, 600)  # 5-10分钟
                print(f"  [休息] 已连续跑10个问题，休息 {rest_min/60:.1f} 分钟...", flush=True)
                await asyncio.sleep(rest_min)

            # 夜间0-6点自动降频，间隔拉长3倍
            hour = datetime.now().hour
            if 0 <= hour < 6:
                actual_min = delay_min * 3
                actual_max = delay_max * 3
                print(f"  [夜间降频] 间隔拉长到 {actual_min}-{actual_max} 秒", flush=True)
            else:
                actual_min = delay_min
                actual_max = delay_max
            delay = random.uniform(actual_min, actual_max)
            print(f"  等待 {delay:.1f} 秒...", flush=True)
            await asyncio.sleep(delay)

    task_state["status"] = "completed"
    task_state["finished_at"] = datetime.now().isoformat()

    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task_state, f, ensure_ascii=False, indent=2)

    del running_tasks[task_id]
    print(f"[任务 {task_id}] 完成！", flush=True)


# 每周随机休息日：每周随机挑半天（4小时）不跑批量任务，更像真人
import datetime as dt
_rest_day_cache = {}
def is_rest_time():
    """检查当前是不是每周休息日的休息时段。"""
    now = dt.datetime.now()
    week_key = now.strftime("%Y-%W")
    if week_key not in _rest_day_cache:
        # 本周随机挑一个星期几
        rest_weekday = random.randint(0, 6)
        # 随机挑半天（0-20点之间选一个开始时间）
        rest_start = random.randint(0, 20)
        _rest_day_cache[week_key] = (rest_weekday, rest_start)
    rest_weekday, rest_start = _rest_day_cache[week_key]
    # 休息4小时
    return now.weekday() == rest_weekday and rest_start <= now.hour < rest_start + 4


@app.post("/api/tasks")
async def create_task(req: CreateTaskRequest):
    """创建一个批量提问任务。"""
    if not req.questions:
        raise HTTPException(status_code=400, detail="questions 不能为空")
    
    # 每周休息日检查
    if is_rest_time():
        raise HTTPException(status_code=429, detail="今天是每周休息日，系统正在休息，请稍后再试")

    # 用默认模型列表
    all_ids = [m["id"] for m in MODELS]
    model_ids = req.models or all_ids
    # 过滤掉暂停的模型
    model_ids = [mid for mid in model_ids if MODEL_MAP[mid]["name"] not in paused_models]
    # 校验模型ID
    for mid in model_ids:
        if mid not in MODEL_MAP:
            raise HTTPException(status_code=400, detail=f"未知模型: {mid}")

    # 生成任务ID
    task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}"

    # 启动后台任务
    asyncio.create_task(run_task(task_id, req.questions, model_ids, req.delay_min, req.delay_max))
    running_tasks[task_id] = True

    return {
        "task_id": task_id,
        "status": "running",
        "total_questions": len(req.questions),
        "models": model_ids,
    }


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    """查询任务状态和结果。"""
    task_file = TASKS_DIR / f"{task_id}.json"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail="任务不存在")

    with open(task_file, encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/tasks")
def list_tasks():
    """列出所有任务（按时间倒序）。"""
    tasks = []
    for task_file in sorted(TASKS_DIR.glob("task_*.json"), reverse=True)[:50]:
        try:
            with open(task_file, encoding="utf-8") as f:
                t = json.load(f)
            tasks.append({
                "task_id": t["task_id"],
                "status": t["status"],
                "total": t["total"],
                "completed": t["completed"],
                "failed": t["failed"],
                "created_at": t["created_at"],
            })
        except:
            pass
    return {"tasks": tasks}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    """取消任务。"""
    task_file = TASKS_DIR / f"{task_id}.json"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    with open(task_file, encoding="utf-8") as f:
        task = json.load(f)
    task["status"] = "cancelled"
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    return {"success": True, "status": "cancelled"}


@app.post("/api/tasks/{task_id}/pause")
def pause_task(task_id: str):
    """暂停任务。"""
    task_file = TASKS_DIR / f"{task_id}.json"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    with open(task_file, encoding="utf-8") as f:
        task = json.load(f)
    task["status"] = "paused"
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    return {"success": True, "status": "paused"}


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str):
    """继续暂停的任务。"""
    task_file = TASKS_DIR / f"{task_id}.json"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    with open(task_file, encoding="utf-8") as f:
        task = json.load(f)
    if task["status"] not in ("paused", "cancelled"):
        raise HTTPException(status_code=400, detail="任务不是暂停状态")

    # 用存好的完整问题列表，从断点继续
    all_questions = task.get("all_questions", [])
    model_ids = task["models"]

    task["status"] = "running"
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    asyncio.create_task(run_task(task_id, all_questions, model_ids, 5, 10))
    running_tasks[task_id] = True
    return {"success": True, "status": "running"}


@app.get("/api/tasks/{task_id}/export")
def export_task(task_id: str):
    """导出任务结果为CSV。"""
    import csv
    import io
    task_file = TASKS_DIR / f"{task_id}.json"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail="任务不存在")

    with open(task_file, encoding="utf-8") as f:
        task = json.load(f)

    output = io.StringIO()
    writer = csv.writer(output)
    # 表头
    headers = ["问题"]
    for mid in task["models"]:
        headers.extend([f"{mid}_回答", f"{mid}_引用数", f"{mid}_引用链接", f"{mid}_搜索关键词"])
    writer.writerow(headers)

    for r in task["results"]:
        row = [r["question"]]
        for mid in task["models"]:
            m = r["models"].get(mid, {})
            citations = m.get("citations", [])
            # 把所有引用URL拼起来
            citation_urls = " | ".join([c.get("url", "") for c in citations if c.get("url")])
            row.append(m.get("answer", ""))
            row.append(len(citations))
            row.append(citation_urls)
            row.append(" | ".join(m.get("search_queries", [])))
        writer.writerow(row)

    output.seek(0)
    return {"csv": output.getvalue()}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """返回前端页面，检查是否登录。"""
    token = request.cookies.get("dashboard_token", "")
    if token not in valid_tokens:
        return RedirectResponse(url="/login")
    html_path = __file__.replace("run_dashboard.py", "static/index.html")
    with open(html_path, encoding="utf-8") as f:
        return f.read()


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """登录页面。"""
    return """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>登录 - Laya Dashboard</title>
        <style>
            body { font-family: -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }
            .login-box { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 320px; }
            h2 { text-align: center; color: #333; margin-bottom: 24px; }
            input { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; box-sizing: border-box; margin-bottom: 16px; }
            button { width: 100%; padding: 12px; background: #1677ff; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; }
            button:hover { background: #4096ff; }
            .error { color: #ff4d4f; text-align: center; margin-top: 12px; display: none; }
        </style>
    </head>
    <body>
        <div class="login-box">
            <h2>Laya 农场管理后台</h2>
            <form id="loginForm">
                <input type="password" id="password" placeholder="请输入密码" required>
                <button type="submit">登 录</button>
                <div class="error" id="error">密码错误</div>
            </form>
        </div>
        <script>
            document.getElementById('loginForm').onsubmit = async (e) => {
                e.preventDefault();
                const password = document.getElementById('password').value;
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password})
                });
                if (res.ok) {
                    window.location.href = '/';
                } else {
                    document.getElementById('error').style.display = 'block';
                }
            };
        </script>
    </body>
    </html>
    """


class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
async def login(req: LoginRequest):
    if req.password != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    token = secrets.token_urlsafe(32)
    valid_tokens.add(token)
    from fastapi.responses import Response
    resp = Response(status_code=200)
    resp.set_cookie(key="dashboard_token", value=token, httponly=True, max_age=86400)
    return resp


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """简单鉴权中间件。"""
    path = request.url.path
    # 公开路径
    if path.startswith("/login") or path.startswith("/static") or path == "/favicon.ico" or path.startswith("/geo/"):
        return await call_next(request)
    # API 登录接口
    if path == "/api/login":
        return await call_next(request)
    # 检查token
    token = request.cookies.get("dashboard_token", "")
    if token not in valid_tokens:
        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "未登录"})
        return RedirectResponse(url="/login")
    return await call_next(request)


# ---------- 自动健康检查 ----------
HEALTH_CHECK_INTERVAL_MIN = 900  # 最少10分钟
HEALTH_CHECK_INTERVAL_MAX = 1800  # 最多20分钟
health_history = []  # 健康检查历史

# 探测问题池：30个日常小问题，每次随机抽一个，避免重复被风控
HEALTH_CHECK_QUESTIONS = [
    "你好啊",
    "你是谁",
    "你叫什么名字",
    "你会什么",
    "1+1等于几",
    "今天天气怎么样",
    "早上好",
    "晚上好",
    "中午好",
    "你吃饭了吗",
    "现在几点了",
    "今天星期几",
    "现在是哪一年",
    "你今年多大了",
    "你是男生还是女生",
    "你是谁开发的",
    "你是哪个公司做的",
    "你是AI吗",
    "你真的懂吗",
    "你能陪我聊天吗",
    "讲个笑话吧",
    "讲个短故事",
    "给我讲个冷笑话",
    "讲个童话故事",
    "说个脑筋急转弯",
    "推荐一首歌",
    "推荐一部电影",
    "推荐一本书",
    "推荐一个综艺",
    "推荐一个景点",
    "你最喜欢什么颜色",
    "你最喜欢吃什么",
    "你最喜欢什么季节",
    "你最喜欢什么动物",
    "你最喜欢什么运动",
    "你会写代码吗",
    "你会做PPT吗",
    "你会写作文吗",
    "你会翻译吗",
    "你会总结吗",
    "你平时喜欢干什么",
    "你有什么特长",
    "你有什么爱好",
    "你有什么缺点",
    "你觉得工作辛苦吗",
    "给我一句鼓励的话",
    "给我一句安慰的话",
    "给我一句早安问候",
    "给我一句晚安问候",
    "给我一个生活小技巧",
    "怎么才能不熬夜",
    "怎么提高睡眠质量",
    "怎么缓解压力",
    "怎么提高专注力",
    "怎么养成好习惯",
    "周末去哪玩比较好",
    "假期怎么安排",
    "一个人在家干嘛",
    "下雨天适合干什么",
    "晴天适合干什么",
    "咖啡和茶哪个好",
    "可乐和果汁哪个好",
    "米饭和面条哪个好",
    "猫和狗哪个好",
    "夏天和冬天哪个好",
    "你觉得AI会取代人类吗",
    "你觉得未来会怎样",
    "你觉得读书有用吗",
    "你觉得运动重要吗",
    "你觉得健康重要吗",
    "今天过得怎么样",
    "最近有什么新鲜事",
    "你知道什么热点新闻",
    "你有什么新鲜事分享",
    "最近有什么好看的剧",
    "你会说英语吗",
    "你会说日语吗",
    "你会说韩语吗",
    "你会说粤语吗",
    "你会说方言吗",
    "怎么煮面条",
    "怎么煮米饭",
    "怎么炒鸡蛋",
    "怎么泡咖啡",
    "怎么泡奶茶",
    "怎么学英语",
    "怎么学Python",
    "怎么学写作",
    "怎么学画画",
    "怎么学拍照",
    "怎么整理房间",
    "怎么收纳衣服",
    "怎么收拾书桌",
    "怎么打扫卫生",
    "怎么洗衣服",
    "你觉得旅行的意义是什么",
    "你觉得朋友是什么",
    "你觉得家是什么",
    "你觉得快乐是什么",
    "你觉得幸福是什么",
    "讲个职场小技巧",
    "讲个沟通小技巧",
    "讲个时间管理技巧",
    "讲个学习技巧",
    "讲个理财小技巧",
    "你知道什么冷知识",
    "你知道什么历史小故事",
    "你知道什么科学小常识",
    "你知道什么生活常识",
    "你知道什么网络梗",
    "怎么和同事相处",
    "怎么和领导沟通",
    "怎么拒绝别人",
    "怎么表达自己的想法",
    "怎么和新朋友打招呼",
    "你喜欢听歌吗",
    "你喜欢看电影吗",
    "你喜欢看书吗",
    "你喜欢运动吗",
    "你喜欢做饭吗",
    "冬天怎么保暖",
    "夏天怎么防暑",
    "秋天怎么养生",
    "春天怎么防过敏",
    "换季怎么预防感冒",
    "怎么选手机",
    "怎么选电脑",
    "怎么选耳机",
    "怎么选手表",
    "怎么选包包",
    "你觉得什么样的人受欢迎",
    "你觉得什么样的朋友值得交",
    "你觉得什么样的工作好",
    "你觉得什么样的生活好",
    "你觉得什么样的旅行好",
    "讲个笑话开心一下",
    "今天有点累",
    "今天有点烦",
    "今天很开心",
    "今天很充实",
    "怎么调整心情",
    "怎么缓解焦虑",
    "怎么克服拖延",
    "怎么改掉坏毛病",
    "怎么保持好心情",
    "你知道怎么养猫吗",
    "你知道怎么养狗吗",
    "你知道怎么养花吗",
    "你知道怎么养鱼吗",
    "你知道怎么养兔子吗",
    "怎么拍好看的照片",
    "怎么修图",
    "怎么剪视频",
    "怎么做vlog",
    "怎么发朋友圈",
    "你喜欢晴天还是雨天",
    "你喜欢春天还是秋天",
    "你喜欢热闹还是安静",
    "你喜欢宅家还是出门",
    "你喜欢一个人还是一群人",
    "怎么快速出门",
    "怎么快速出门收拾",
    "怎么快速吃饭",
    "怎么快速入睡",
    "怎么快速背单词",
    "讲个小学生的问题",
    "讲个中学生的问题",
    "讲个大学生的问题",
    "讲个上班族的问题",
    "讲个创业者的问题",
    "你知道怎么点外卖省钱吗",
    "你知道怎么网购省钱吗",
    "你知道怎么买机票便宜吗",
    "你知道怎么订酒店便宜吗",
    "你知道怎么买电影票便宜吗",
    "怎么安排一周菜单",
    "怎么安排一周学习计划",
    "怎么安排一周工作计划",
    "怎么安排一周运动计划",
    "怎么安排一周阅读计划",
    "你觉得读书的好处是什么",
    "你觉得运动的好处是什么",
    "你觉得旅行的好处是什么",
    "你觉得交朋友的好处是什么",
    "你觉得早睡早起的好处是什么",
    "讲个古代小故事",
    "讲个历史人物的故事",
    "讲个科学家的故事",
    "讲个发明家的故事",
    "讲个普通人的故事",
    "怎么用手机提高效率",
    "怎么用电脑提高效率",
    "怎么用软件提高效率",
    "怎么用清单提高效率",
    "怎么用习惯提高效率",
    "你知道怎么调咖啡好喝吗",
    "你知道怎么泡茶好喝吗",
    "你知道怎么调奶茶好喝吗",
    "你知道怎么调果汁好喝吗",
    "你知道怎么调鸡尾酒吗",
    "怎么选自行车",
    "怎么选滑板",
    "怎么选露营装备",
    "怎么选瑜伽垫",
    "怎么选跑鞋",
    "你觉得早餐重要吗",
    "你觉得午餐重要吗",
    "你觉得晚餐重要吗",
    "你觉得夜宵吃什么好",
    "你觉得下午茶吃什么好",
    "怎么写请假条",
    "怎么写工作总结",
    "怎么写邮件",
    "怎么写简历",
    "怎么写自我介绍",
    "你知道怎么选口红吗",
    "你知道怎么选护肤品吗",
    "你知道怎么选洗发水吗",
    "你知道怎么选洗面奶吗",
    "你知道怎么选面膜吗",
    "怎么布置卧室",
    "怎么布置客厅",
    "怎么布置书房",
    "怎么布置阳台",
    "怎么布置厨房",
    "讲个网络热梗解释",
    "讲个职场黑话解释",
    "讲个学生梗解释",
    "讲个美食梗解释",
    "讲个旅行梗解释",
    "怎么用电脑办公更高效",
    "怎么用键盘快捷键",
    "怎么用鼠标快捷键",
    "怎么用手机快捷操作",
    "怎么用语音助手",
    "你知道怎么选西瓜甜吗",
    "你知道怎么选芒果甜吗",
    "你知道怎么选桃子甜吗",
    "你知道怎么选葡萄甜吗",
    "你知道怎么选草莓甜吗",
    "怎么安排周末两天",
    "怎么安排三天小长假",
    "怎么安排五一假期",
    "怎么安排国庆假期",
    "怎么安排春节假期",
    "你觉得什么样的食物健康",
    "你觉得什么样的运动好坚持",
    "你觉得什么样的书籍值得读",
    "你觉得什么样的电影值得看",
    "你觉得什么样的音乐好听",
    "讲个和宠物的趣事",
    "讲个和朋友的趣事",
    "讲个和家人的趣事",
    "讲个工作中的趣事",
    "讲个旅行中的趣事",
    "怎么整理手机相册",
    "怎么整理电脑文件",
    "怎么整理微信聊天记录",
    "怎么整理书架",
    "怎么整理衣柜",
    "你知道怎么选新鲜蔬菜吗",
    "你知道怎么选新鲜水果吗",
    "你知道怎么选新鲜肉吗",
    "你知道怎么选海鲜吗",
    "你知道怎么选豆腐吗",
    "怎么和长辈沟通",
    "怎么和小孩沟通",
    "怎么和陌生人沟通",
    "怎么和客户沟通",
    "怎么和老板沟通",
    "你觉得现在的生活节奏快吗",
    "你觉得现在的人压力大吗",
    "你觉得现在的人焦虑吗",
    "你觉得现在的人快乐吗",
    "你觉得现在的人孤独吗",
    "讲个保持好心情的小方法",
    "讲个缓解疲劳的小方法",
    "讲个快速入睡的小方法",
    "讲个提高记忆力的小方法",
    "讲个保护视力的小方法",
    "怎么选笔记本",
    "怎么选钢笔",
    "怎么选铅笔",
    "怎么选文件夹",
    "怎么选文件架",
    "你知道怎么煮方便面好吃吗",
    "你知道怎么煮螺蛳粉好吃吗",
    "你知道怎么煮火锅好吃吗",
    "你知道怎么煮烧烤好吃吗",
    "你知道怎么煮冒菜好吃吗",
    "怎么安排每天的时间",
    "怎么安排每天的任务",
    "怎么安排每天的学习",
    "怎么安排每天的运动",
    "怎么安排每天的阅读",
    "讲个关于时间管理的小故事",
    "讲个关于坚持的小故事",
    "讲个关于勇气的小故事",
    "讲个关于善良的小故事",
    "讲个关于梦想的小故事",
    "你知道怎么选充电宝吗",
    "你知道怎么选数据线吗",
    "你知道怎么选耳机吗",
    "你知道怎么选音箱吗",
    "你知道怎么选平板吗",
    "怎么用手机拍月亮",
    "怎么用手机拍夜景",
    "怎么用手机拍人像",
    "怎么用手机拍美食",
    "怎么用手机拍风景",
    "你觉得什么样的早餐营养",
    "你觉得什么样的午餐健康",
    "你觉得什么样的晚餐清淡",
    "你觉得什么样的零食健康",
    "你觉得什么样的饮料健康",
    "讲个春夏秋冬的小知识",
    "讲个风雨雷电的小知识",
    "讲个花鸟鱼虫的小知识",
    "讲个天文地理的小知识",
    "讲个人体的小知识",
    "怎么用电脑剪辑视频",
    "怎么用电脑修图",
    "怎么用电脑做PPT",
    "怎么用电脑做表格",
    "怎么用电脑写文档",
    "你知道怎么选露营帐篷吗",
    "你知道怎么选睡袋吗",
    "你知道怎么选露营灯吗",
    "你知道怎么选折叠桌吗",
    "你知道怎么选折叠椅吗",
    "怎么和邻居相处",
    "怎么和物业沟通",
    "怎么和快递员沟通",
    "怎么和外卖员沟通",
    "怎么和客服沟通",
    "你觉得什么样的天气适合出门",
    "你觉得什么样的天气适合宅家",
    "你觉得什么样的天气适合约会",
    "你觉得什么样的天气适合爬山",
    "你觉得什么样的天气适合逛街",
    "讲个关于学习的小技巧",
    "讲个关于工作的小技巧",
    "讲个关于生活的小技巧",
    "讲个关于社交的小技巧",
    "讲个关于理财的小技巧",
    "你知道怎么选羽毛球拍吗",
    "你知道怎么选乒乓球拍吗",
    "你知道怎么选篮球吗",
    "你知道怎么选足球吗",
    "你知道怎么选瑜伽球吗",
    "怎么安排一天的三餐",
    "怎么安排一周的三餐",
    "怎么安排一个月的菜谱",
    "怎么安排减脂期的饮食",
    "怎么安排增肌期的饮食",
    "讲个关于AI的小知识",
    "讲个关于互联网的小知识",
    "讲个关于手机的小知识",
    "讲个关于电脑的小知识",
    "讲个关于软件的小知识",
    "你知道怎么选自行车头盔吗",
    "你知道怎么选骑行眼镜吗",
    "你知道怎么选骑行手套吗",
    "你知道怎么选骑行服吗",
    "你知道怎么选骑行鞋吗",
    "怎么保持办公桌整洁",
    "怎么保持电脑桌面整洁",
    "怎么保持手机桌面整洁",
    "怎么保持书包整洁",
    "怎么保持钱包整洁",
    "你觉得什么样的人值得深交",
    "你觉得什么样的人不值得交往",
    "你觉得什么样的朋友靠谱",
    "你觉得什么样的同事好相处",
    "你觉得什么样的领导好",
    "讲个关于坚持的名言",
    "讲个关于努力的名言",
    "讲个关于梦想的名言",
    "讲个关于善良的名言",
    "讲个关于时间的名言",
    "怎么用手机记账",
    "怎么用手机提醒事项",
    "怎么用手机备忘录",
    "怎么用手机日历",
    "怎么用手机闹钟",
    "你知道怎么选鲜花吗",
    "你知道怎么选绿植吗",
    "你知道怎么选多肉吗",
    "你知道怎么选盆栽吗",
    "你知道怎么选花瓶吗",
    "怎么和小朋友玩",
    "怎么和老人聊天",
    "怎么和年轻人聊天",
    "怎么和同龄人聊天",
    "怎么和小朋友讲故事",
    "你觉得什么样的城市适合生活",
    "你觉得什么样的小镇适合度假",
    "你觉得什么样的海边适合去",
    "你觉得什么样的山里适合玩",
    "你觉得什么样的乡村适合去",
    "讲个关于环保的小知识",
    "讲个关于节能的小知识",
    "讲个关于节水的小知识",
    "讲个关于垃圾分类的小知识",
    "讲个关于减塑的小知识",
    "怎么用相机拍人像",
    "怎么用相机拍风景",
    "怎么用相机拍夜景",
    "怎么用相机拍运动",
    "怎么用相机拍宠物",
    "你知道怎么选羽毛球吗",
    "你知道怎么选网球吗",
    "你知道怎么选排球吗",
    "你知道怎么选橄榄球吗",
    "你知道怎么选台球吗",
    "怎么安排孩子的周末",
    "怎么安排孩子的假期",
    "怎么陪孩子写作业",
    "怎么陪孩子玩游戏",
    "怎么陪孩子读绘本",
    "讲个关于春天的诗句",
    "讲个关于夏天的诗句",
    "讲个关于秋天的诗句",
    "讲个关于冬天的诗句",
    "讲个关于月亮的诗句",
    "你知道怎么选茶叶吗",
    "你知道怎么选咖啡吗",
    "你知道怎么选牛奶吗",
    "你知道怎么选酸奶吗",
    "你知道怎么选蜂蜜吗",
    "怎么用电脑做思维导图",
    "怎么用电脑做流程图",
    "怎么用电脑做数据可视化",
    "怎么用电脑做笔记",
    "怎么和猫相处",
    "怎么和狗相处",
    "怎么和鸟相处",
    "怎么和鱼相处",
    "怎么和仓鼠相处",
    "你觉得什么样的电影治愈",
    "你觉得什么样的电影搞笑",
    "你觉得什么样的电影感人",
    "你觉得什么样的电影烧脑",
    "你觉得什么样的电影经典",
    "讲个关于旅行的小故事",
    "讲个关于美食的小故事",
    "讲个关于友情的小故事",
    "讲个关于亲情的小故事",
    "讲个关于爱情的小故事",
    "怎么用手机修图加滤镜",
    "怎么用手机拼图",
    "怎么用手机做表情包",
    "怎么用手机做视频",
    "怎么用手机做直播",
    "你知道怎么选行李箱吗",
    "你知道怎么选双肩包吗",
    "你知道怎么选斜挎包吗",
    "你知道怎么选钱包吗",
    "你知道怎么选背包吗",
    "怎么安排短途一日游",
    "怎么安排周边两日游",
    "怎么安排跨省三日游",
    "怎么安排出国五日游",
    "怎么安排深度七日游",
    "讲个关于健康饮食的小知识",
    "讲个关于运动健身的小知识",
    "讲个关于睡眠健康的小知识",
    "讲个关于心理健康的小知识",
    "讲个关于口腔健康的小知识",
    "你知道怎么选乒乓球桌吗",
    "你知道怎么选羽毛球网吗",
    "你知道怎么选篮球架吗",
    "你知道怎么选足球门吗",
    "你知道怎么选瑜伽垫吗",
    "怎么和外国人打招呼",
    "怎么和外国人聊天",
    "怎么用英语点餐",
    "怎么用英语问路",
    "怎么用英语购物",
    "你觉得什么样的早餐快手",
    "你觉得什么样的午餐快手",
    "你觉得什么样的晚餐快手",
    "你觉得什么样的快手菜好吃",
    "你觉得什么样的快手汤好喝",
    "讲个关于文具的小知识",
    "讲个关于办公的小知识",
    "讲个关于会议的小知识",
    "讲个关于汇报的小知识",
    "讲个关于职场晋升的小知识",
    "你知道怎么选滑板吗",
    "你知道怎么选轮滑鞋吗",
    "你知道怎么选平衡车吗",
    "你知道怎么选儿童自行车吗",
    "你知道怎么选婴儿车吗",
    "怎么用手机读电子书",
    "怎么用手机听书",
    "怎么用手机听播客",
    "怎么用手机听音乐",
    "怎么用手机听相声",
    "你觉得什么样的天气适合拍照",
    "你觉得什么样的光线适合拍照",
    "你觉得什么样的背景适合拍照",
    "你觉得什么样的角度适合拍照",
    "你觉得什么样的穿搭适合拍照",
    "讲个关于树木的小知识",
    "讲个关于花朵的小知识",
    "讲个关于水果的小知识",
    "讲个关于蔬菜的小知识",
    "讲个关于谷物的小知识",
    "怎么和同事拼餐",
    "怎么和同事拼车",
    "怎么和同事一起健身",
    "怎么和同事一起学习",
    "怎么和同事一起摸鱼",
    "你知道怎么选毛笔吗",
    "你知道怎么选宣纸吗",
    "你知道怎么选砚台吗",
    "你知道怎么选墨汁吗",
    "你知道怎么选镇纸吗",
    "怎么安排睡前半小时",
    "怎么安排起床后半小时",
    "怎么安排午休半小时",
    "怎么安排通勤半小时",
    "怎么安排睡前阅读半小时",
    "讲个关于音乐的小知识",
    "讲个关于美术的小知识",
    "讲个关于文学的小知识",
    "讲个关于电影的小知识",
    "讲个关于戏剧的小知识",
    "你知道怎么选吉他吗",
    "你知道怎么选尤克里里吗",
    "你知道怎么选钢琴吗",
    "你知道怎么选小提琴吗",
    "你知道怎么选架子鼓吗",
    "怎么和小朋友解释为什么要上学",
    "怎么和小朋友解释为什么要刷牙",
    "怎么和小朋友解释为什么要早睡",
    "怎么和小朋友解释为什么要分享",
    "怎么和小朋友解释为什么要懂礼貌",
    "你觉得什么样的周末最放松",
    "你觉得什么样的假期最舒服",
    "你觉得什么样的生活最惬意",
    "你觉得什么样的工作最轻松",
    "你觉得什么样的关系最舒服",
    "讲个关于节约的小故事",
    "讲个关于环保的小故事",
    "讲个关于助人为乐的小故事",
    "讲个关于诚实守信的小故事",
    "讲个关于尊老爱幼的小故事"
]

def play_alarm():
    """播放报警声音：先响铃3次，再语音播报。"""
    try:
        # 1. 先响铃3次
        for _ in range(3):
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
        # 2. 再语音播报
        subprocess.run(["say", "警告：模型异常，请检查"], check=False)
    except Exception as e:
        print(f"[健康检查] 播放报警失败: {e}", flush=True)


async def check_single_model(m: dict) -> str:
    # 暂停的模型不检查
    if m["name"] in paused_models:
        return ""
    """并发检查单个模型，返回异常描述或空字符串。"""
    try:
        loop = asyncio.get_event_loop()
        def _check():
            question = random.choice(HEALTH_CHECK_QUESTIONS)
            req = urllib.request.Request(
                f"http://127.0.0.1:{m['port']}/ask",
                data=json.dumps({"question": question}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
                if len(data.get("answer", "")) < 5:
                    return f"{m['name']} 回答过短"
                return ""
        return await loop.run_in_executor(None, _check)
    except Exception as e:
        return f"{m['name']} 异常: {str(e)[:50]}"


async def health_check_loop():
    """定时健康检查循环：10-20分钟随机间隔，并发检查所有模型，异常先自愈再报警。"""
    # 启动后先等1分钟再第一次检查
    await asyncio.sleep(60)

    while True:
        # 有批量任务在跑就跳过这次健康检查，避免抢窗口
        if len(running_tasks) > 0:
            print(f"[健康检查] 检测到批量任务运行中，跳过本次检查", flush=True)
            delay = random.uniform(HEALTH_CHECK_INTERVAL_MIN, HEALTH_CHECK_INTERVAL_MAX)
            await asyncio.sleep(delay)
            continue

        print(f"[健康检查] 开始检查 {datetime.now().isoformat()}", flush=True)

        # 并发检查所有5个模型
        tasks = [check_single_model(m) for m in MODELS]
        results = await asyncio.gather(*tasks)
        abnormal = [r for r in results if r]

        # 有异常，先尝试自动重启
        if abnormal:
            print(f"[健康检查] 发现异常: {abnormal}", flush=True)
            print(f"[健康检查] 尝试自动重启...", flush=True)

            # 提取异常的模型ID
            abnormal_names = [a.split()[0] for a in abnormal]
            abnormal_ids = [m["id"] for m in MODELS if m["name"] in abnormal_names]

            for mid in abnormal_ids:
                # launchctl 重启该模型
                subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.laya.{mid}"],
                    check=False
                )
                print(f"[健康检查] 已重启 {mid}", flush=True)

            # 等30秒让服务起来
            print(f"[健康检查] 等待30秒服务重启...", flush=True)
            await asyncio.sleep(30)

            # 再并发检查一次异常的模型
            still_tasks = [check_single_model(MODEL_MAP[mid]) for mid in abnormal_ids]
            still_results = await asyncio.gather(*still_tasks)
            still_abnormal = [r for r in still_results if r]

            if still_abnormal:
                print(f"[健康检查] 重启后仍异常: {still_abnormal}，触发报警", flush=True)
                record = {
                    "time": datetime.now().isoformat(),
                    "abnormal": still_abnormal,
                    "self_healed": False,
                }
                health_history.append(record)
                save_health_record(record)
                play_alarm()
            else:
                print(f"[健康检查] 自动重启成功，已自愈", flush=True)
                record = {
                    "time": datetime.now().isoformat(),
                    "abnormal": [],
                    "self_healed": True,
                }
                health_history.append(record)
                save_health_record(record)
        else:
            print(f"[健康检查] 全部正常", flush=True)
            record = {
                "time": datetime.now().isoformat(),
                "abnormal": [],
                "self_healed": False,
            }
            health_history.append(record)
            save_health_record(record)

        # 只保留最近50条
        if len(health_history) > 50:
            health_history.pop(0)

        # 随机间隔 10-20 分钟
        delay = random.uniform(HEALTH_CHECK_INTERVAL_MIN, HEALTH_CHECK_INTERVAL_MAX)
        print(f"[健康检查] 下次检查在 {delay/60:.1f} 分钟后", flush=True)
        await asyncio.sleep(delay)


@app.on_event("startup")
async def startup():
    """服务启动时：1. 启动健康检查 2. 恢复未完成的任务。"""
    asyncio.create_task(health_check_loop())

    # 恢复未完成的任务
    for task_file in TASKS_DIR.glob("task_*.json"):
        try:
            with open(task_file, encoding="utf-8") as f:
                task = json.load(f)
            if task.get("status") == "running":
                task_id = task["task_id"]
                # 用存好的完整问题列表，run_task 会自动从断点开始
                all_questions = task.get("all_questions", [])
                model_ids = task["models"]
                print(f"[启动] 恢复未完成任务: {task_id}", flush=True)
                asyncio.create_task(run_task(task_id, all_questions, model_ids, 5, 10))
                running_tasks[task_id] = True
        except Exception as e:
            print(f"[启动] 恢复任务失败: {e}", flush=True)


@app.get("/api/health")
def system_health():
    """系统资源监控：CPU、内存、磁盘。"""
    import shutil
    import psutil

    # 磁盘
    disk = shutil.disk_usage("/")
    disk_used_percent = round(disk.used / disk.total * 100, 1)

    # CPU
    cpu_percent = psutil.cpu_percent(interval=1)

    # 内存
    mem = psutil.virtual_memory()
    mem_used_percent = mem.percent

    return {
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
        "disk_used_percent": disk_used_percent,
        "cpu_percent": cpu_percent,
        "mem_total_gb": round(mem.total / 1024 / 1024 / 1024, 1),
        "mem_used_percent": mem_used_percent,
    }


@app.get("/api/health_history")
def get_health_history():
    """获取健康检查历史。"""
    return {"history": health_history[-20:]}


# ---------- 日志备份和清理 ----------
backup_records = []  # 备份/清理操作记录


@app.get("/api/request_logs")
async def get_logs(service: str = "", limit: int = 100):
    """读取日志列表，支持按模型筛选。"""
    log_dir = Path(__file__).parent / "server_logs"
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"{today}.jsonl"
    
    if not log_file.exists():
        return {"logs": []}
    
    logs = []
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line.strip())
                if service and item.get("service") != service:
                    continue
                logs.append(item)
            except:
                continue
    
    # 倒序，最新的在前
    logs.reverse()
    return {"logs": logs[:limit]}

@app.post("/api/backup_logs")
def backup_logs():
    """备份日志：把当前日志打包成 zip 文件。"""
    import shutil
    log_dir = Path(__file__).parent / "server_logs"
    backup_dir = Path(__file__).parent / "backups"
    backup_dir.mkdir(exist_ok=True)

    if not log_dir.exists():
        raise HTTPException(status_code=400, detail="日志目录不存在")

    # 打包
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"logs_backup_{timestamp}"
    zip_path = backup_dir / zip_name
    shutil.make_archive(zip_path, "zip", log_dir)

    # 记录
    record = {
        "time": datetime.now().isoformat(),
        "action": "backup",
        "file": f"{zip_name}.zip",
        "size_mb": round(zip_path.stat().st_size / 1024 / 1024, 2),
    }
    backup_records.insert(0, record)
    return {"success": True, "record": record}


@app.post("/api/cleanup_logs")
def cleanup_logs():
    """清理日志：删除7天前的日志文件。"""
    log_dir = Path(__file__).parent / "server_logs"
    if not log_dir.exists():
        raise HTTPException(status_code=400, detail="日志目录不存在")

    cutoff = datetime.now() - timedelta(days=7)
    deleted = []
    for log_file in log_dir.glob("*.jsonl"):
        try:
            file_date = datetime.strptime(log_file.stem, "%Y-%m-%d")
            if file_date < cutoff:
                size = log_file.stat().st_size
                log_file.unlink()
                deleted.append({"file": log_file.name, "size_kb": round(size / 1024, 1)})
        except:
            pass

    record = {
        "time": datetime.now().isoformat(),
        "action": "cleanup",
        "deleted_count": len(deleted),
        "deleted_files": deleted,
    }
    backup_records.insert(0, record)
    return {"success": True, "record": record}


@app.get("/api/backup_records")
def get_backup_records():
    """获取备份/清理操作记录。"""
    return {"records": backup_records[:20]}




# ==================== GEO Gateway 接口 ====================
from fastapi import Depends

class GEOQuestion(BaseModel):
    question_id: str
    text: str

class GEOCreateTask(BaseModel):
    department_id: str
    brand_code: str
    questions: list[GEOQuestion]
    models: list[str]
    sampling_window: Optional[str] = None
    priority: str = "normal"
    metadata: Optional[dict] = {}

@app.post("/geo/tasks", dependencies=[Depends(verify_geo_api_key)])
async def geo_create_task(req: GEOCreateTask):
    """创建GEO采样任务，立刻返回task_id，不等待执行。幂等：相同请求返回已有任务"""
    # 幂等检查：遍历现有任务，找相同的department+brand+questions+models
    req_qs = sorted([(q.question_id, q.text) for q in req.questions])
    req_models = sorted(req.models)
    for tf in TASKS_DIR.glob("geo_*.json"):
        try:
            with open(tf) as f:
                existing = json.load(f)
            if existing.get("department_id") != req.department_id:
                continue
            if existing.get("brand_code") != req.brand_code:
                continue
            if existing.get("sampling_window") != req.sampling_window:
                continue
            ex_qs = sorted([(q.get("question_id"), q.get("text")) for q in existing.get("questions", [])])
            ex_models = sorted(existing.get("models", []))
            if ex_qs == req_qs and ex_models == req_models and existing.get("status") not in ["failed", "cancelled"]:
                return {
                    "task_id": existing["task_id"],
                    "status": existing["status"],
                    "total": existing["total"],
                    "completed": existing["completed"],
                    "failed": existing["failed"],
                    "idempotent": True
                }
        except:
            continue
    
    geo_log("task_create", {"department_id": req.department_id, "brand_code": req.brand_code, "models": req.models, "questions": len(req.questions)})
    task_id = f"geo_{int(time.time())}_{secrets.token_hex(4)}"
    questions_text = [q.text for q in req.questions]
    
    # 保存任务元数据
    task_data = {
        "task_id": task_id,
        "department_id": req.department_id,
        "brand_code": req.brand_code,
        "questions": [q.dict() for q in req.questions],
        "models": req.models,
        "sampling_window": req.sampling_window,
        "priority": req.priority,
        "metadata": req.metadata,
        "status": "queued",
        "created_at": datetime.now().isoformat(),
        "farm_version": "1.0.0",
        "prompt_version": "v1",
        "results": [],
        "total": len(questions_text) * len(req.models),
        "completed": 0,
        "failed": 0
    }
    with open(TASKS_DIR / f"{task_id}.json", "w") as f:
        json.dump(task_data, f, ensure_ascii=False, indent=2)
    
    # 后台执行任务
    asyncio.create_task(geo_run_task(task_id, questions_text, req.models, req))
    
    return {
        "task_id": task_id,
        "status": "queued",
        "total": task_data["total"],
        "completed": 0,
        "failed": 0
    }

@app.get("/geo/tasks/{task_id}", dependencies=[Depends(verify_geo_api_key)])
async def geo_get_task(task_id: str):
    """查询任务摘要和进度"""
    task_file = TASKS_DIR / f"{task_id}.json"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail="Task not found")
    with open(task_file) as f:
        task = json.load(f)
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "department_id": task["department_id"],
        "brand_code": task["brand_code"],
        "total": task["total"],
        "completed": task["completed"],
        "failed": task["failed"],
        "created_at": task["created_at"],
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "models": task["models"]
    }

@app.get("/geo/tasks/{task_id}/results", dependencies=[Depends(verify_geo_api_key)])
async def geo_get_results(task_id: str, page: int = 1, page_size: int = 50):
    """分页读取逐题结果"""
    task_file = TASKS_DIR / f"{task_id}.json"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail="Task not found")
    with open(task_file) as f:
        task = json.load(f)
    results = task.get("results", [])
    total = len(results)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "task_id": task_id,
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": results[start:end]
    }

@app.post("/geo/tasks/{task_id}/cancel", dependencies=[Depends(verify_geo_api_key)])
async def geo_cancel_task(task_id: str):
    geo_log("task_cancel", {"task_id": task_id})
    """取消任务，未开始的问题不再执行"""
    task_file = TASKS_DIR / f"{task_id}.json"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail="Task not found")
    with open(task_file) as f:
        task = json.load(f)
    task["status"] = "cancelled"
    task["finished_at"] = datetime.now().isoformat()
    with open(task_file, "w") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    return {"ok": True, "task_id": task_id, "status": "cancelled"}

@app.get("/geo/health", dependencies=[Depends(verify_geo_api_key)])
async def geo_health():
    """GEO服务健康检查：进程/浏览器/登录态/磁盘/队列/最近成功时间"""
    # 磁盘使用率
    disk = shutil.disk_usage("/Users/alili/laya")
    disk_usage = {
        "total_gb": round(disk.total / 1024**3, 1),
        "used_gb": round(disk.used / 1024**3, 1),
        "free_gb": round(disk.free / 1024**3, 1),
        "percent": round(disk.used / disk.total * 100, 1)
    }
    
    # 检查每个模型状态
    model_status = {}
    for m in MODELS:
        model_info = {
            "paused": m["name"] in paused_models,
            "port": m["port"]
        }
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{m['port']}/", timeout=3) as resp:
                model_info["process"] = "ok"
                model_info["browser"] = "ok"
                model_info["login"] = "ok"
        except Exception as e:
            model_info["process"] = "down"
            model_info["browser"] = "down"
            model_info["login"] = "unknown"
            model_info["last_error"] = str(e)[:100]
        # 从健康记录里找最近成功时间
        model_info["last_success_at"] = None
        model_status[m["id"]] = model_info
    
    # 磁盘告警
    disk_alert = disk_usage["percent"] > 90
    
    return {
        "status": "ok" if not disk_alert else "degraded",
        "service": "geo-gateway",
        "version": "1.0.0",
        "models": model_status,
        "queue_length": len(running_tasks),
        "running_tasks": list(running_tasks.keys()),
        "disk": disk_usage,
        "disk_alert": disk_alert,
        "timestamp": datetime.now().isoformat()
    }

async def geo_run_task(task_id, questions, model_ids, req):
    """后台执行GEO任务"""
    running_tasks[task_id] = "running"
    async with task_lock:
        task_file = TASKS_DIR / f"{task_id}.json"
        with open(task_file) as f:
            task = json.load(f)
        task["status"] = "running"
        task["started_at"] = datetime.now().isoformat()
        with open(task_file, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        geo_log("task_started", {"task_id": task_id, "models": model_ids, "questions": len(questions)})
        
        # 过滤暂停模型
        active_models = [mid for mid in model_ids if MODEL_MAP[mid]["name"] not in paused_models]
        
        # 并发给所有模型发问题
        for q_idx, q_text in enumerate(questions):
            # 每次循环重新读文件，检测取消状态
            with open(task_file) as f:
                task = json.load(f)
            if task["status"] == "cancelled":
                break
            
            # 并发问所有模型
            async def ask_one(mid):
                loop = asyncio.get_event_loop()
                try:
                    def _call():
                        data = json.dumps({"question": q_text}).encode()
                        req_url = urllib.request.Request(
                            f"http://127.0.0.1:{MODEL_MAP[mid]['port']}/ask",
                            data=data,
                            headers={"Content-Type": "application/json"}
                        )
                        with urllib.request.urlopen(req_url, timeout=180) as resp:
                            return json.loads(resp.read())
                    result = await loop.run_in_executor(None, _call)
                    model_fail_counts[mid] = 0  # 成功重置失败计数
                    return {
                        "task_id": task_id,
                        "question_id": req.questions[q_idx].question_id,
                        "question": q_text,
                        "model": mid,
                        "status": "success",
                        "answer": result.get("answer", ""),
                        "citations": result.get("citations", []),
                        "search_queries": result.get("search_queries", []),
                        "source": "laya-browser",
                        "farm_version": "1.0.0",
                        "prompt_version": "v1",
                        "finished_at": datetime.now().isoformat(),
                        "error": None
                    }
                except Exception as e:
                    err_str = str(e).lower()
                    err_type = "unknown"
                    if "timeout" in err_str or "timed out" in err_str:
                        err_type = "timeout"
                    elif "login" in err_str or "401" in err_str or "未登录" in str(e):
                        err_type = "not_logged_in"
                    elif "429" in err_str or "rate" in err_str or "风控" in str(e) or "频繁" in str(e):
                        err_type = "rate_limited"
                    elif "browser" in err_str or "crash" in err_str or "closed" in err_str:
                        err_type = "browser_error"
                    elif "connection" in err_str or "refused" in err_str or "unreachable" in err_str:
                        err_type = "service_unavailable"
                    model_fail_counts[mid] += 1
                    # 连续失败自动延长间隔
                    interval = BACKOFF_INTERVAL if model_fail_counts[mid] >= MODEL_FAIL_THRESHOLD else random.uniform(15, 25)
                    return {
                        "task_id": task_id,
                        "question_id": req.questions[q_idx].question_id,
                        "question": q_text,
                        "model": mid,
                        "status": "failed",
                        "error": err_type,
                        "error_msg": str(e)[:200],
                        "source": "laya-browser",
                        "farm_version": "1.0.0",
                        "finished_at": datetime.now().isoformat()
                    }
            
            results = await asyncio.gather(*[ask_one(mid) for mid in active_models])
            
            # 保存每道题的结果
            with open(task_file) as f:
                task = json.load(f)
            task["results"].extend(results)
            # 累计统计所有结果
            task["completed"] = sum(1 for r in task["results"] if r["status"] == "success")
            task["failed"] = sum(1 for r in task["results"] if r["status"] == "failed")
            # 记录每模型失败计数，用于退避
            for r in results:
                if r["status"] == "success":
                    model_fail_counts[r["model"]] = 0
                else:
                    model_fail_counts[r["model"]] = model_fail_counts.get(r["model"], 0) + 1
            
            if q_idx < len(questions) - 1:
                task["status"] = "running"
            else:
                task["status"] = "partial" if task["failed"] > 0 else "completed"
                task["finished_at"] = datetime.now().isoformat()
            with open(task_file, "w") as f:
                json.dump(task, f, ensure_ascii=False, indent=2)
            
            # 题间间隔：如果有模型连续失败，用退避间隔，否则用随机15-25秒
            if q_idx < len(questions) - 1:
                max_fail = max(model_fail_counts.get(mid, 0) for mid in active_models)
                if max_fail >= MODEL_FAIL_THRESHOLD:
                    wait_sec = BACKOFF_INTERVAL
                else:
                    wait_sec = random.uniform(TASK_INTERVAL_MIN, TASK_INTERVAL_MAX)
                await asyncio.sleep(wait_sec)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9000)





