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
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional
from pydantic import BaseModel
import secrets

app = FastAPI(title="Laya Dashboard")

# Dashboard 密码
DASHBOARD_PASSWORD = "beijixing1"
# 简单token存储（内存里，重启失效）
valid_tokens = set()

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
PAUSE_FILE = BASE_DIR / "paused_models.json"

def load_paused():
    if PAUSE_FILE.exists():
        with open(PAUSE_FILE) as f:
            return set(json.load(f))
    return set()

def save_paused(paused):
    with open(PAUSE_FILE, "w") as f:
        json.dump(list(paused), f)

paused_models = load_paused()

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

# 任务存储目录
TASKS_DIR = Path(__file__).parent / "tasks"
TASKS_DIR.mkdir(exist_ok=True)

# 内存中运行中的任务状态
running_tasks = {}
# 全局任务锁：同一时间只跑一个批量任务，避免抢窗口
task_lock = asyncio.Lock()


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

        # 依次调用每个模型，带重试
        for mid in model_ids:
            print(f"  -> {mid} 提问中...", flush=True)
            # 重试2次
            result = None
            for attempt in range(2):
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, call_model, mid, question)
                if result["status"] == "success":
                    break
                print(f"  -> {mid} 第{attempt+1}次失败，重试...", flush=True)
                await asyncio.sleep(3)

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
    if path.startswith("/login") or path.startswith("/static") or path == "/favicon.ico":
        return await call_next(request)
    # API 登录接口
    if path == "/api/login":
        return await call_next(request)
    # 检查token
    token = request.cookies.get("dashboard_token", "")
    if token not in valid_tokens:
        if path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="未登录")
        return RedirectResponse(url="/login")
    return await call_next(request)


# ---------- 自动健康检查 ----------
HEALTH_CHECK_INTERVAL_MIN = 900  # 最少10分钟
HEALTH_CHECK_INTERVAL_MAX = 1800  # 最多20分钟
health_history = []  # 健康检查历史

# 探测问题池：30个日常小问题，每次随机抽一个，避免重复被风控
HEALTH_CHECK_QUESTIONS = [
    "今天天气怎么样？",
    "你叫什么名字？",
    "1+1等于几？",
    "你会什么？",
    "给我讲个笑话吧",
    "早上好",
    "你好啊",
    "现在几点了？",
    "你是谁开发的？",
    "能陪我聊聊天吗？",
    "推荐一首好听的歌",
    "你最喜欢什么颜色？",
    "吃饭了吗？",
    "周末去哪玩比较好？",
    "你有什么特长？",
    "简单介绍一下你自己",
    "你是AI吗？",
    "讲个短故事吧",
    "今天过得怎么样？",
    "你会说英语吗？",
    "推荐一部好看的电影",
    "你有多聪明？",
    "你知道现在是哪年吗？",
    "你能做什么事情？",
    "给我一句鼓励的话",
    "你觉得工作辛苦吗？",
    "你会写代码吗？",
    "介绍一下你自己",
    "你平时喜欢干什么？",
    "简单说一下你能干嘛",
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
                play_alarm()
            else:
                print(f"[健康检查] 自动重启成功，已自愈", flush=True)
                record = {
                    "time": datetime.now().isoformat(),
                    "abnormal": [],
                    "self_healed": True,
                }
                health_history.append(record)
        else:
            print(f"[健康检查] 全部正常", flush=True)
            record = {
                "time": datetime.now().isoformat(),
                "abnormal": [],
                "self_healed": False,
            }
            health_history.append(record)

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9000)

