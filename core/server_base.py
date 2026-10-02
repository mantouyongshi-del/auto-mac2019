"""
FastAPI 应用工厂：所有模型服务共享的 HTTP 接口骨架。
接收一个 BrowserBase 实例，自动提供 /ask、/batch_ask、/ 健康检查、日志落盘。
"""
import os
import asyncio
import json
from datetime import datetime, timedelta
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


def _auto_pause(service_name: str, reason: str = "captcha"):
    """自动暂停：写入 paused_models.json（与 runner/dashboard 共用，按 model_id）。
    reason 同步记录到 paused_reasons.json：captcha=人工验证，quota_exhausted=当日额度用尽，
    switch_failed=切号失败未登录；fix_runner 只自动恢复 captcha 类，quota 类等跨天额度重置后恢复。"""
    pf = Path(__file__).parent.parent / "paused_models.json"
    rf = Path(__file__).parent.parent / "paused_reasons.json"
    try:
        data = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else []
        if service_name not in data:
            data.append(service_name)
            pf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                reasons = json.loads(rf.read_text(encoding="utf-8")) if rf.exists() else {}
                reasons[service_name] = reason
                rf.write_text(json.dumps(reasons, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            print(f"⚠️ [{service_name}] 已自动暂停模型（原因: {reason}）", flush=True)
    except Exception as e:
        print(f"[auto_pause] 失败: {e}", flush=True)


def _auto_unpause(service_name: str):
    """多账号切换成功后解除暂停：从 paused_models.json 移除该模型，并清理暂停原因。"""
    pf = Path(__file__).parent.parent / "paused_models.json"
    rf = Path(__file__).parent.parent / "paused_reasons.json"
    try:
        data = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else []
        if service_name in data:
            data.remove(service_name)
            pf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                reasons = json.loads(rf.read_text(encoding="utf-8")) if rf.exists() else {}
                reasons.pop(service_name, None)
                rf.write_text(json.dumps(reasons, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            print(f"✅ [{service_name}] 已自动切换备用账号，解除暂停", flush=True)
    except Exception as e:
        print(f"[auto_unpause] 失败: {e}", flush=True)


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
    
    record["timestamp"] = datetime.now().astimezone().isoformat()
    record["service"] = service_name
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- 模型冷却（防风控：每 N 次成功提问强制休息 M 秒） ----------
# 仅当环境变量 ASK_COUNT_RESET > 0 时启用（DeepSeek 配置 50 次 / 30 分钟；
# 其他模型不设置 → 完全关闭，零影响）。
def _cooldown_file(service_name: str):
    return Path(f"/tmp/laya_{service_name}_cooldown.json")


def _load_cooldown(service_name: str) -> dict:
    try:
        return json.load(open(_cooldown_file(service_name), encoding="utf-8"))
    except Exception:
        return {"ask_count": 0, "cooldown_until": None}


def _save_cooldown(service_name: str, st: dict):
    try:
        with open(_cooldown_file(service_name), "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


# ---------- 账号日限额（每号每天最多 N 次成功提问，达到后自动切到未满账号） ----------
# 仅当 ASK_SWITCH_LIMIT > 0 且模型配置了 ACCOUNT_PROFILES 时启用（DeepSeek 70；
# 其他模型不设置 → 完全关闭，零影响）。计数按 profile+日期 独立持久化，
# 双号轮换不会因切号而丢失，跨天自动清零。
def _account_usage_file(service_name: str):
    return Path(f"/tmp/laya_{service_name}_account_usage.json")


def _today_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _load_account_usage(service_name: str) -> dict:
    try:
        d = json.load(open(_account_usage_file(service_name), encoding="utf-8"))
        if d.get("date") != _today_str():
            return {"date": _today_str(), "counts": {}, "used_today": []}
        d.setdefault("counts", {})
        d.setdefault("used_today", [])
        return d
    except Exception:
        return {"date": _today_str(), "counts": {}, "used_today": []}


def _save_account_usage(service_name: str, usage: dict):
    try:
        with open(_account_usage_file(service_name), "w", encoding="utf-8") as f:
            json.dump(usage, f)
    except Exception:
        pass


# ---------- 账号池（accounts.json 为权威）：动态决定可用账号 ----------
# 封禁中的账号自动剔除；ban_until 到期自动恢复可用；active 直接可用。
# 每个账号绑定登录所在的 profile（登录后由台账维护）。
ACCOUNT_POOL_FILE = Path(__file__).resolve().parent.parent / "accounts.json"


def _load_available_profiles(service_name: str) -> list:
    """返回该模型当前可用（非封禁中/已到期恢复）的 profile 路径列表。"""
    try:
        data = json.load(open(ACCOUNT_POOL_FILE, encoding="utf-8"))
    except Exception:
        return []
    now = datetime.now().astimezone()
    profiles = []
    for a in data:
        if a.get("model") != service_name or not a.get("profile"):
            continue
        status = a.get("status", "")
        if status == "active":
            profiles.append(a["profile"])
        elif status in ("banned", "pending") and a.get("ban_until"):
            try:
                t = datetime.strptime(a["ban_until"], "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
                if now >= t:
                    profiles.append(a["profile"])
            except Exception:
                pass
    return profiles


def _pick_switch_target(service_name: str, browser: BrowserBase, usage: dict,
                        ask_switch_limit: int, require_unused_only: bool = False):
    """从账号池选切号目标：今天未启用（每号每天一轮）；满额切号额外要求目标未满。
    返回 profile 路径或 None。"""
    cur_name = Path(browser._current_profile_dir()).name
    candidates = _load_available_profiles(service_name) or [
        p for p in getattr(browser, "ACCOUNT_PROFILES", [])
    ]
    used = usage.get("used_today", [])
    for p in candidates:
        name = Path(p).name
        if name == cur_name or name in used:
            continue
        if not require_unused_only and usage["counts"].get(name, 0) >= ask_switch_limit:
            continue
        return p
    return None


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

    # 冷却配置（DeepSeek: ASK_COUNT_RESET=50, ASK_COOLDOWN_SEC=1800）
    ask_reset = int(os.environ.get("ASK_COUNT_RESET", "0") or 0)
    ask_cooldown = int(os.environ.get("ASK_COOLDOWN_SEC", "0") or 0)
    cooldown_state = _load_cooldown(service_name) if ask_reset > 0 else {"ask_count": 0, "cooldown_until": None}
    # 账号日限额（DeepSeek: ASK_SWITCH_LIMIT=70，达到自动切号）
    ask_switch_limit = int(os.environ.get("ASK_SWITCH_LIMIT", "0") or 0)

    def _in_cooldown() -> bool:
        """是否处于强制休息期（跨服务重启持久化，按文件时间戳判断）"""
        cu = cooldown_state.get("cooldown_until")
        if not cu:
            return False
        try:
            return datetime.fromisoformat(cu) > datetime.now().astimezone()
        except Exception:
            return False

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
            "ask_count": cooldown_state.get("ask_count", 0),
            "cooldown_until": cooldown_state.get("cooldown_until"),
            "account_usage": _load_account_usage(service_name)["counts"] if ask_switch_limit > 0 else {},
            "ask_switch_limit": ask_switch_limit,
        }

    @app.post("/switch_account", dependencies=[Depends(verify_api_key)])
    async def switch_account_endpoint():
        """未登录/验证码场景：从账号池自动切换到"今天未启用"的账号。
        切换成功（新账号已登录）→ 解除暂停；失败 → 保持现状等待人工登录新号。"""
        if not getattr(browser, "ACCOUNT_PROFILES", []):
            return {"switched": False, "reason": "该模型未配置备用账号"}
        usage = _load_account_usage(service_name)
        target = _pick_switch_target(service_name, browser, usage, ask_switch_limit, require_unused_only=True)
        if target is None:
            # 无今天未启用的可用账号 → 退化为顺序切换下一个（原逻辑）
            switched = await browser.switch_account()
        else:
            switched = await browser.switch_account_to(target)
        if switched:
            cur_name = Path(browser._current_profile_dir()).name
            if cur_name not in usage["used_today"]:
                usage["used_today"].append(cur_name)
            _save_account_usage(service_name, usage)
            _auto_unpause(service_name)
        return {"switched": switched}

    @app.post("/ask", dependencies=[Depends(verify_api_key)])
    async def ask(req: AskRequest):
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="问题不能为空")
        # 强制休息期：拒绝提问，由 runner 识别后跳过（不计失败、不重试）
        if ask_reset > 0 and _in_cooldown():
            raise HTTPException(
                status_code=429,
                detail=f"模型冷却休息中（每{ask_reset}题强制休息）",
            )
        await lock.acquire(priority=req.priority)
        try:
            runtime_state["busy"] = True
            # 账号日限额检查：当前账号今日已满 → 自动切到账号池中"今天未启用且未满"的号
            if ask_switch_limit > 0 and getattr(browser, "ACCOUNT_PROFILES", []):
                usage = _load_account_usage(service_name)
                cur_name = Path(browser._current_profile_dir()).name
                cur_count = usage["counts"].get(cur_name, 0)
                if cur_count >= ask_switch_limit:
                    target = _pick_switch_target(service_name, browser, usage, ask_switch_limit, require_unused_only=False)
                    if target is None:
                        _save_account_usage(service_name, usage)
                        _auto_pause(service_name, "quota_exhausted")
                        raise HTTPException(
                            status_code=429,
                            detail=f"所有可用账号今日额度已用尽（每号{ask_switch_limit}次），已暂停等待次日",
                        )
                    switched = await browser.switch_account_to(target)
                    if not switched:
                        _save_account_usage(service_name, usage)
                        _auto_pause(service_name, "switch_failed")
                        raise HTTPException(
                            status_code=429,
                            detail=f"账号{cur_name}今日已满，切换{Path(target).name}失败（未登录），已暂停等待人工",
                        )
                    # 当前号今天已启用一轮，切走后当天不再切回
                    if cur_name not in usage["used_today"]:
                        usage["used_today"].append(cur_name)
                    _save_account_usage(service_name, usage)
                    print(
                        f"[{service_name}] 账号 {cur_name} 今日已达 {ask_switch_limit} 次，自动切号 -> {Path(target).name}",
                        flush=True,
                    )
                _save_account_usage(service_name, usage)
            try:
                result = await asyncio.wait_for(browser.ask(req.question), timeout=260)
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
                # 人工验证：多账号模型（DeepSeek）先自动切换备用账号；无备用账号则暂停+报警
                if getattr(browser, "ACCOUNT_PROFILES", []):
                    switched = await browser.switch_account()
                    if switched:
                        _auto_unpause(service_name)
                        save_log({
                            "question": req.question,
                            "error": "人工验证，已自动切换备用账号并恢复",
                            "answer": "",
                            "citations": [],
                            "search_queries": [],
                        }, service_name)
                        raise HTTPException(status_code=503,
                                             detail=f"{service_name} 人工验证，已自动切换备用账号")
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
                # 未登录/账号失效(401)且支持多账号 → 自动切换备用账号
                if e.status_code == 401 and getattr(browser, "ACCOUNT_PROFILES", []):
                    switched = await browser.switch_account()
                    if switched:
                        _auto_unpause(service_name)
                        save_log({
                            "question": req.question,
                            "error": f"{e.detail}，已自动切换备用账号并恢复",
                            "answer": "",
                            "citations": [],
                            "search_queries": [],
                        }, service_name)
                        raise HTTPException(status_code=503,
                                             detail=f"{service_name} 未登录，已自动切换备用账号")
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

        # 成功路径清零失败计数（失败路径由 _bump_fail 累计，达阈值触发页面重置）
        _reset_fail(service_name)
        result = {
            "question": req.question,
            "answer": result["answer"],
            "citations": result["citations"],
            "search_queries": result.get("search_queries", []),
        }
        runtime_state["last_success_at"] = datetime.now().astimezone().isoformat()
        # 冷却计数：成功提问 +1，达到阈值 → 进入强制休息期（跨重启持久化）
        if ask_reset > 0:
            cooldown_state["ask_count"] = cooldown_state.get("ask_count", 0) + 1
            if cooldown_state["ask_count"] >= ask_reset:
                cooldown_state["ask_count"] = 0
                cooldown_state["cooldown_until"] = (
                    datetime.now().astimezone() + timedelta(seconds=ask_cooldown)
                ).isoformat()
                print(
                    f"[{service_name}] 已连续成功 {ask_reset} 题，进入强制休息 "
                    f"{ask_cooldown // 60} 分钟",
                    flush=True,
                )
            _save_cooldown(service_name, cooldown_state)
        # 账号日限额计数：成功提问按当前 profile 累加；记录"今天已启用"（每号每天一轮）
        if ask_switch_limit > 0:
            usage = _load_account_usage(service_name)
            cur_name = Path(browser._current_profile_dir()).name
            usage["counts"][cur_name] = usage["counts"].get(cur_name, 0) + 1
            if cur_name not in usage["used_today"]:
                usage["used_today"].append(cur_name)
            _save_account_usage(service_name, usage)
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
                    r = await asyncio.wait_for(browser.ask(q), timeout=260)
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
