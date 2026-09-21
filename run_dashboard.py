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
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from typing import Optional
from pydantic import BaseModel

app = FastAPI(title="Laya Dashboard")

# 五个模型配置
MODELS = [
    {"id": "deepseek", "name": "DeepSeek", "port": 8000},
    {"id": "qianwen", "name": "千问", "port": 8001},
    {"id": "doubao", "name": "豆包", "port": 8002},
    {"id": "wenxin", "name": "文心一言", "port": 8003},
    {"id": "yuanbao", "name": "元宝", "port": 8004},
]
MODEL_MAP = {m["id"]: m for m in MODELS}

# 任务存储目录
TASKS_DIR = Path(__file__).parent / "tasks"
TASKS_DIR.mkdir(exist_ok=True)

# 内存中运行中的任务状态
running_tasks = {}


class CreateTaskRequest(BaseModel):
    questions: list
    models: Optional[list] = None
    delay_min: int = 5
    delay_max: int = 10
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

    for i in range(start_index, len(questions)):
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
            delay = random.uniform(delay_min, delay_max)
            print(f"  等待 {delay:.1f} 秒...", flush=True)
            await asyncio.sleep(delay)

    task_state["status"] = "completed"
    task_state["finished_at"] = datetime.now().isoformat()

    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task_state, f, ensure_ascii=False, indent=2)

    del running_tasks[task_id]
    print(f"[任务 {task_id}] 完成！", flush=True)


@app.post("/api/tasks")
async def create_task(req: CreateTaskRequest):
    """创建一个批量提问任务。"""
    if not req.questions:
        raise HTTPException(status_code=400, detail="questions 不能为空")

    # 用默认模型列表
    model_ids = req.models or [m["id"] for m in MODELS]
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
        headers.extend([f"{mid}_回答", f"{mid}_引用数", f"{mid}_搜索关键词"])
    writer.writerow(headers)

    for r in task["results"]:
        row = [r["question"]]
        for mid in task["models"]:
            m = r["models"].get(mid, {})
            row.append(m.get("answer", ""))
            row.append(len(m.get("citations", [])))
            row.append(" | ".join(m.get("search_queries", [])))
        writer.writerow(row)

    output.seek(0)
    return {"csv": output.getvalue()}


@app.get("/", response_class=HTMLResponse)
async def index():
    """返回前端页面。"""
    html_path = __file__.replace("run_dashboard.py", "static/index.html")
    with open(html_path, encoding="utf-8") as f:
        return f.read()


# ---------- 自动健康检查 ----------
HEALTH_CHECK_INTERVAL_MIN = 600  # 最少10分钟
HEALTH_CHECK_INTERVAL_MAX = 1200  # 最多20分钟
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


async def health_check_loop():
    """定时健康检查循环：每5-10分钟随机间隔，发轻量问题探测，异常先自愈再报警。"""
    # 启动后先等1分钟再第一次检查
    await asyncio.sleep(60)

    while True:
        print(f"[健康检查] 开始检查 {datetime.now().isoformat()}", flush=True)
        abnormal = []

        for m in MODELS:
            try:
                # 从问题池随机抽一个，60秒超时
                question = random.choice(HEALTH_CHECK_QUESTIONS)
                req = urllib.request.Request(
                    f"http://127.0.0.1:{m['port']}/ask",
                    data=json.dumps({"question": question}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                    # 检查回答长度，太短可能有问题
                    if len(data.get("answer", "")) < 5:
                        abnormal.append(f"{m['name']} 回答过短")
            except Exception as e:
                abnormal.append(f"{m['name']} 异常: {str(e)[:50]}")

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

            # 再检查一次
            still_abnormal = []
            for mid in abnormal_ids:
                m = MODEL_MAP[mid]
                try:
                    question = random.choice(HEALTH_CHECK_QUESTIONS)
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{m['port']}/ask",
                        data=json.dumps({"question": question}).encode(),
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        data = json.loads(resp.read())
                        if len(data.get("answer", "")) < 5:
                            still_abnormal.append(f"{m['name']} 重启后仍异常")
                except Exception as e:
                    still_abnormal.append(f"{m['name']} 重启后仍失败: {str(e)[:50]}")

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
