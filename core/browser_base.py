"""
浏览器自动化基类：所有模型（DeepSeek/千问/豆包...）共享的通用骨架。

子类只需配置类属性 + 实现模型特有的选择器和提取逻辑，
主流程（新会话→输入→发送→等待→提取）由本类通用完成。
"""
import asyncio
import json
import math
import random
import subprocess
import time
from pathlib import Path
from playwright.async_api import async_playwright
from fastapi import HTTPException

# 项目根目录（所有模型共用 profile / 日志的根）
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
LOG_DIR = PROJECT_ROOT / "server_logs"


class CaptchaDetected(Exception):
    """检测到人工验证（验证码/滑块）时抛出，触发暂停+报警。"""


class BrowserBase:
    # ---------- 子类必须配置的类属性 ----------
    URL = ""                              # 目标网页
    PROFILE_DIR = ""                      # Chrome 持久化 profile 绝对路径
    INPUT_SELECTOR = "textarea"           # 输入框选择器
    ANSWER_BLOCK_SELECTOR = ""            # 回答正文块选择器
    WAIT_TIMEOUT = 120                     # 等待回答最长秒数
    HEADLESS = False                      # 首次登录需 False 看到浏览器
    # 窗口位置和大小（x, y, width, height），每个模型自己配
    WINDOW_POS = (0, 0)
    WINDOW_SIZE = (1280, 900)
    # ---- DeepSeek 专属增强开关（其他模型默认关闭，行为零变化）----
    STEALTH_EXTRA = False        # True=额外注入 playwright-stealth 补丁（仅该模型生效）
    HUMANIZE_LEVEL = 0           # >=1 时启用贝塞尔鼠标+更慢输入节奏（仅该模型生效）
    ACCOUNT_PROFILES = []        # 多账号冗余：如 ["profiles/deepseek", "profiles/deepseek_bak"]，空=单账号

    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self.captured_responses = {}  # URL -> body text（网络拦截捕获）
        # 多账号轮换：当前 profile 下标持久化到 /tmp，服务重启后保持上次切到的账号，
        # 避免重启又退回主 profile（主号封禁时会再次失效）。
        self._profile_idx = self._load_profile_idx()   # 当前使用的账号 profile 下标（ACCOUNT_PROFILES）
        self._mouse_pos = None        # 鼠标最后位置（贝塞尔轨迹起点）

    # ---------- 多账号 profile 持久化 ----------
    def _active_profile_file(self) -> Path:
        name = Path(self.PROFILE_DIR).name if self.PROFILE_DIR else self.__class__.__name__
        return Path(f"/tmp/laya_{name}_active_profile.json")

    def _load_profile_idx(self) -> int:
        try:
            return int(json.load(open(self._active_profile_file())).get("idx", 0) or 0)
        except Exception:
            return 0

    def _save_profile_idx(self):
        try:
            with open(self._active_profile_file(), "w") as f:
                json.dump({"idx": self._profile_idx}, f)
        except Exception:
            pass

    # ---------- 通用：启动 / 关闭 ----------
    def _current_profile_dir(self) -> Path:
        """多账号模式下返回当前 profile 目录，单账号返回 PROFILE_DIR。"""
        profiles = getattr(self, "ACCOUNT_PROFILES", [])
        if profiles:
            idx = getattr(self, "_profile_idx", 0)
            if 0 <= idx < len(profiles):
                return PROJECT_ROOT / profiles[idx]
        return self.PROFILE_DIR

    async def start(self):
        profile_dir = self._current_profile_dir()
        # 启动前清理Chrome锁文件，避免异常退出后下次启动失败
        lock_files = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
        for f in lock_files:
            lock_path = profile_dir / f
            if lock_path.exists():
                try:
                    lock_path.unlink()
                    print(f"[启动] 清理锁文件: {f}", flush=True)
                except Exception:
                    pass
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=self.HEADLESS,
            chromium_sandbox=True,  # 禁止playwright自动加--no-sandbox（消除顶部横幅+自动化特征）
            ignore_default_args=["--disable-blink-features=AutomationControlled"],  # 阻止playwright自动注入该flag（顶部横幅来源）
            viewport={"width": self.WINDOW_SIZE[0], "height": self.WINDOW_SIZE[1]},
            args=[
                # 注：webdriver 等自动化特征已由下方 add_init_script 隐藏，无需 AutomationControlled flag
                "--disable-features=IsolateOrigins,site-per-process",
                # 禁用"恢复之前的页面"崩溃提示气泡
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                # 禁用系统通知弹窗
                "--disable-notifications",
                # 禁用Chrome命令行标记提示条
                "--disable-infobars",
                # 窗口位置和大小
                f"--window-position={self.WINDOW_POS[0]},{self.WINDOW_POS[1]}",
                f"--window-size={self.WINDOW_SIZE[0]},{self.WINDOW_SIZE[1]}",
            ],
        )
        # 反检测：隐藏 webdriver 等自动化痕迹
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: ['zh-CN', 'zh', 'en']});
            window.chrome = {runtime: {}};
        """)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        # DeepSeek 专属：额外注入 playwright-stealth 补丁（必须在页面加载前；懒加载避免其他模型依赖）
        if getattr(self, "STEALTH_EXTRA", False):
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(self.page)
                print("[stealth] playwright-stealth 补丁已注入（该模型专属）", flush=True)
            except Exception as e:
                print(f"[stealth] 注入失败（不影响运行）: {e}", flush=True)
        await self.page.goto(self.URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)  # 等页面加载完，窗口标题出来

        # 移动窗口到指定位置
        title_keyword = getattr(self, "WINDOW_TITLE_KEYWORD", None)
        if title_keyword:
            self._move_window(title_keyword)
        
        # 启动后先关掉所有可能的弹窗
        await asyncio.sleep(2)
        await self.dismiss_popups()
        # 关掉Chrome顶部提示条
        await asyncio.sleep(0.5)
        self.dismiss_chrome_infobar()

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception as e:
                print(f"[browser] context.close() 异常: {e}", flush=True)
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as e:
                print(f"[browser] playwright.stop() 异常: {e}", flush=True)

    async def switch_account(self) -> bool:
        """多账号轮换：切换到下一个 profile 并重启浏览器。
        仅 ACCOUNT_PROFILES 配置了 >=2 个账号的模型（DeepSeek）生效。
        返回是否已登录（备份账号未登录时返回 False，不解除暂停）。"""
        profiles = getattr(self, "ACCOUNT_PROFILES", [])
        if len(profiles) < 2:
            return False
        cur = getattr(self, "_profile_idx", 0)
        new_idx = (cur + 1) % len(profiles)
        if new_idx == cur:
            return False
        return await self._switch_to(new_idx)

    async def switch_account_to(self, target: str) -> bool:
        """切换到指定 profile（如 "profiles/deepseek_bak"）并重启浏览器。
        用于"账号日限额用尽→切到指定未满账号"。目标就是当前账号时只做登录检测。"""
        profiles = getattr(self, "ACCOUNT_PROFILES", [])
        if target not in profiles:
            return False
        idx = profiles.index(target)
        if idx == getattr(self, "_profile_idx", 0):
            try:
                return bool(await asyncio.wait_for(self.is_logged_in(), timeout=10))
            except Exception:
                return False
        return await self._switch_to(idx)

    async def _switch_to(self, new_idx: int) -> bool:
        """切到指定下标 profile：关浏览器→清理目标残留进程→重启→检测登录。"""
        profiles = getattr(self, "ACCOUNT_PROFILES", [])
        self._profile_idx = new_idx
        self._save_profile_idx()  # 持久化当前账号，服务重启后保持
        print(f"[账号] 切换 profile -> {profiles[new_idx]}", flush=True)
        try:
            await self.close()
        except Exception as e:
            print(f"[账号] 关闭旧浏览器异常: {e}", flush=True)
        self.playwright = None
        self.context = None
        self.page = None
        self.captured_responses = {}
        self._mouse_pos = None
        # 防残留：登录窗口/旧实例可能仍占用目标 profile 目录，先杀掉再启动，
        # 否则 launch_persistent_context 会报"正在现有的浏览器会话中打开"而失败。
        try:
            import subprocess
            target_dir = (Path(__file__).resolve().parent.parent / profiles[new_idx]).resolve()
            subprocess.run(
                ["pkill", "-f", f"user-data-dir={target_dir}"],
                capture_output=True,
                timeout=10,
            )
            await asyncio.sleep(2)
            for f in target_dir.glob("Singleton*"):
                try:
                    f.unlink()
                except Exception:
                    pass
            print(f"[账号] 已清理目标 profile 残留进程/锁: {profiles[new_idx]}", flush=True)
        except Exception as e:
            print(f"[账号] 清理目标残留警告: {e}", flush=True)
        try:
            await self.start()
            logged = await asyncio.wait_for(self.is_logged_in(), timeout=10)
            print(f"[账号] 切换{'成功（已登录）' if logged else '完成但备份账号未登录'}", flush=True)
            return logged
        except Exception as e:
            print(f"[账号] 切换启动失败: {e}", flush=True)
            return False

    # ---------- 子类必须实现 ----------
    CAPTCHA_URL_KEYWORDS = ("captcha", "verify", "security", "nvc", "safecheck", "slider")
    CAPTCHA_TEXT_KEYWORDS = ("请完成验证", "安全验证", "人机验证", "拖动滑块", "向右滑动", "验证码")

    async def _check_captcha(self, strict: bool = False) -> bool:
        """检测当前页面是否出现人工验证（验证码/滑块/安全验证）。
        strict=True（回答完成后二次检测）：只查 URL 与验证 DOM 元素，
        不扫全文正文——正常回答页常含"安全验证/验证码"等文字，全文匹配会误报。"""
        try:
            url = self.page.url.lower()
            if any(k in url for k in self.CAPTCHA_URL_KEYWORDS):
                return True
            # 只查高置信验证元素：verify/slider 等词在正常页面组件中太常见（如滑块组件、轮播），
            # 会误报；captcha/nc_/nvc/yidun/geetest/iframe captcha 是验证特有标识。
            # 注意：很多 AI 站点即使没有验证，页面上也常驻安全 SDK 的隐藏组件
            # （tcaptcha/yidun/nc_/geetest 埋点，常见 1x1、opacity:0、移出视口），
            # is_visible() 对这些仍返回 True。因此除可见性外，还必须满足：
            #   - 有真实 bounding box 且宽高 >= 40px（真验证弹窗/滑块都远大于此）
            #   - 与视口有可见交集（移到屏幕外的不算）
            vp = None
            try:
                vp = self.page.viewport_size
            except Exception:
                pass
            for sel in ("[class*='captcha']", "[class*='nc_']", "iframe[src*='captcha']",
                        "[class*='nvc']", "[class*='yidun']", "[class*='geetest']",
                        "[id*='captcha']", "iframe[src*='verify']"):
                # 所有 playwright 调用带短超时：长回答页面 DOM 重，挂起会阻塞整条 ask 链路
                try:
                    locs = await asyncio.wait_for(self.page.locator(sel).all(), timeout=1.5)
                except Exception:
                    continue
                for loc in locs:
                    try:
                        if not await asyncio.wait_for(loc.is_visible(), timeout=1.5):
                            continue
                        box = await asyncio.wait_for(loc.bounding_box(), timeout=1.5)
                        if not box or box["width"] < 40 or box["height"] < 40:
                            continue  # 极小/无尺寸埋点不算
                        if vp and (box["x"] + box["width"] < 0 or box["y"] + box["height"] < 0
                                   or box["x"] > vp["width"] or box["y"] > vp["height"]):
                            continue  # 完全移出视口不算
                        return True
                    except Exception:
                        continue
            if not strict:
                try:
                    text = await asyncio.wait_for(
                        self.page.locator("body").inner_text(timeout=2000), timeout=3)
                except Exception:
                    text = ""
                if any(k in text for k in self.CAPTCHA_TEXT_KEYWORDS):
                    return True
        except Exception:
            pass
        return False

    async def new_chat(self) -> bool:
        """开启新对话，确保每题独立会话。"""
        raise NotImplementedError

    async def recover_page(self):
        """连续失败后彻底重置页面：刷新 + 等待加载 + 开新会话。

        解决"页面看似正常但回答接口无响应"的持续失败（如元宝连续 502）。
        """
        try:
            print("[恢复] 连续失败，刷新页面重置状态", flush=True)
            await self.page.reload()
            await asyncio.sleep(random.uniform(3, 5))
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(1, 2))
            try:
                await self.new_chat()
            except Exception:
                pass
            print("[恢复] 页面已刷新并开新会话", flush=True)
        except Exception as e:
            print(f"[恢复] 页面刷新失败: {e}", flush=True)

    async def is_logged_in(self) -> bool:
        """检查是否已登录。"""
        raise NotImplementedError

    async def extract_answer(self, base_count: int = 0) -> dict:
        """提取本次新增回答正文，返回 {"text": ..., "html": ...}。"""
        raise NotImplementedError

    async def extract_citations(self, base_count: int = 0) -> list:
        """提取本次回答的引用列表，返回 [{"ref": ..., "url": ...}]。"""
        raise NotImplementedError

    async def extract_search_queries(self, base_count: int = 0) -> list:
        """提取本次回答用了哪些搜索关键词。默认返回空（拿不到的模型）。"""
        return []

    def _url_match(self, url: str) -> bool:
        """判断 URL 是否匹配回答接口模式（ANSWER_URL_PATTERN 的 glob 简化实现）。"""
        pattern = getattr(self, 'ANSWER_URL_PATTERN', None)
        if not pattern:
            return False
        p = pattern.replace("**", "")
        return p in url

    # ---------- 子类可覆盖（有默认值） ----------
    def decorate_question(self, question: str) -> str:
        """装饰要发送给模型的问题文本。默认原样返回，子类可附加提示词。"""
        return question

    async def handle_post_send_popups(self):
        """问题发送后、等待回答前，处理模型特有的弹窗（如元宝的二次确认选择框）。默认空实现。"""
        return None

    def should_capture_url(self, url: str) -> bool:
        """判断是否需要捕获此响应的 body。默认不捕获（DOM 模式）。"""
        return False

    @staticmethod
    def _fix_double_encoding(s: str) -> str:
        """修复 cp1252 双重编码问题（UTF-8 字节被 cp1252 解码后又 UTF-8 编码）。"""
        cp1252_map = {
            '\u20ac': b'\x80', '\u201a': b'\x82', '\u0192': b'\x83',
            '\u201e': b'\x84', '\u2026': b'\x85', '\u2020': b'\x86',
            '\u2021': b'\x87', '\u02c6': b'\x88', '\u2030': b'\x89',
            '\u0160': b'\x8a', '\u2039': b'\x8b', '\u0152': b'\x8c',
            '\u017d': b'\x8e', '\u2018': b'\x91', '\u2019': b'\x92',
            '\u201c': b'\x93', '\u201d': b'\x94', '\u2022': b'\x95',
            '\u2013': b'\x96', '\u2014': b'\x97', '\u02dc': b'\x98',
            '\u2122': b'\x99', '\u0161': b'\x9a', '\u203a': b'\x9b',
            '\u0153': b'\x9c', '\u017e': b'\x9e', '\u0178': b'\x9f',
        }
        result = bytearray()
        for ch in s:
            if ch in cp1252_map:
                result.extend(cp1252_map[ch])
            elif ord(ch) < 256:
                result.append(ord(ch))
            else:
                # 不在 cp1252 范围的字符，直接 UTF-8 编码
                result.extend(ch.encode('utf-8'))
        return bytes(result).decode('utf-8', errors='replace')

    def stop_button_selector(self) -> str:
        """停止生成按钮选择器（存在=仍在生成）。返回空串表示不用此信号。"""
        return ""

    async def maybe_enable_search(self):
        """若该模型有"联网搜索"开关，在此开启。默认什么都不做。"""
        pass

    async def check_login_url(self):
        """发送前检查登录态（URL 跳转登录页则抛 401）。默认不检查。"""
        return

    def _move_window(self, title_keyword: str):
        """用 macOS AppleScript 移动 Chrome 窗口到指定位置。"""
        x, y = self.WINDOW_POS
        w, h = self.WINDOW_SIZE
        script = f'''
        tell application "Google Chrome"
            set targetWin to first window whose title contains "{title_keyword}"
            set bounds of targetWin to {x}, {y}, {x + w}, {y + h}
        end tell
        '''
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
            print(f"[窗口] 已移动 {title_keyword} 到 ({x},{y}) {w}x{h}", flush=True)
        except Exception as e:
            print(f"[窗口] 移动 {title_keyword} 失败: {e}", flush=True)

    # ---------- DeepSeek 专属：拟人化增强（HUMANIZE_LEVEL>=1 才生效）----------
    async def _bezier_move(self, from_pt: tuple, to_pt: tuple, steps: int = 30):
        """贝塞尔曲线鼠标移动：二次曲线 + 随机控制点 + ease 速度 + 微抖动，模拟真人轨迹。"""
        x1, y1 = from_pt
        x2, y2 = to_pt
        dx, dy = x2 - x1, y2 - y1
        dist = math.hypot(dx, dy) or 1
        # 控制点：沿轨迹法向随机偏移（人类轨迹有弧度，不是直线）
        offset = max(30, dist * random.uniform(0.15, 0.4))
        angle = math.atan2(dy, dx) + math.pi / 2
        side = random.choice([-1, 1])
        cx = (x1 + x2) / 2 + math.cos(angle) * offset * side
        cy = (y1 + y2) / 2 + math.sin(angle) * offset * side
        try:
            for i in range(1, steps + 1):
                t = i / steps
                ease = t * t * (3 - 2 * t)  # ease-in-out
                x = (1 - ease) ** 2 * x1 + 2 * (1 - ease) * ease * cx + ease ** 2 * x2
                y = (1 - ease) ** 2 * y1 + 2 * (1 - ease) * ease * cy + ease ** 2 * y2
                # 微抖动 ±2px + 随机停顿
                await self.page.mouse.move(
                    int(x) + random.randint(-2, 2),
                    int(y) + random.randint(-2, 2),
                )
                await asyncio.sleep(random.uniform(0.004, 0.015))
        except Exception:
            pass

    async def _move_mouse(self, x: int, y: int, steps: int = 10):
        """统一鼠标移动入口：HUMANIZE_LEVEL>=1 用贝塞尔轨迹，否则保持原线性移动。"""
        try:
            if getattr(self, "HUMANIZE_LEVEL", 0) >= 1:
                from_pt = getattr(self, "_mouse_pos", None)
                if from_pt is None:
                    # 首次移动：从视口内随机点"把手移过来"
                    vp = self.page.viewport_size or {"width": 1280, "height": 900}
                    from_pt = (random.randint(50, vp["width"] - 50),
                               random.randint(50, vp["height"] - 50))
                await self._bezier_move(from_pt, (x, y), max(steps, 15))
            else:
                await self.page.mouse.move(x, y, steps=steps)
        except Exception:
            try:
                await self.page.mouse.move(x, y, steps=steps)
            except Exception:
                pass
        self._mouse_pos = (x, y)

    def _typing_delay(self) -> float:
        """逐字输入延迟：HUMANIZE_LEVEL>=1 用更慢更自然的节奏。"""
        if getattr(self, "HUMANIZE_LEVEL", 0) >= 1:
            return random.uniform(80, 220)
        return random.uniform(50, 150)

    # ---------- 通用：提问主流程 ----------
    async def ask(self, question: str) -> dict:
        page = self.page

        # 0. 先关掉可能弹出的广告/通知弹窗
        print("[ask] 步骤0 关闭弹窗", flush=True)
        await asyncio.wait_for(self.dismiss_popups(), timeout=20)
        print("[ask] 步骤0 完成", flush=True)

        # 0.5 装饰问题（子类可附加提示词，如豆包禁用工具）
        question = self.decorate_question(question)

        # 1. 每题独立会话
        print("[ask] 步骤1 新会话", flush=True)
        if not await asyncio.wait_for(self.new_chat(), timeout=30):
            raise RuntimeError("开启新会话失败")
        print("[ask] 步骤1 完成", flush=True)

        # 2. 模拟人类：随机停顿
        await asyncio.sleep(random.uniform(0.5, 2.0))

        # 3. 登录检查
        await asyncio.wait_for(self.check_login_url(), timeout=10)
        print("[ask] 步骤3 登录检查完成", flush=True)

        # 4. 开启联网搜索（模型特有）
        await asyncio.wait_for(self.maybe_enable_search(), timeout=15)
        print("[ask] 步骤4 联网搜索完成", flush=True)

        # 5. 逐字输入（优先可见；回退排除 tabindex=-1 的隐藏辅助输入框——文心页面存在此类元素）
        inp = page.locator(self.INPUT_SELECTOR + ":visible").first
        if await inp.count() == 0:
            inp = page.locator(self.INPUT_SELECTOR + ":not([tabindex='-1'])").first
        if await inp.count() == 0:
            inp = page.locator(self.INPUT_SELECTOR).first
        
        # 5.1 模拟真人鼠标移动：先随机移到页面某点，再慢慢移到输入框
        await self._move_mouse(
            random.randint(100, 800),
            random.randint(100, 400),
            steps=random.randint(10, 30)
        )
        await asyncio.sleep(random.uniform(0.1, 0.3))
        # 滚动到视口内，避免 "Element is outside of the viewport"
        try:
            await inp.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            await inp.hover(force=True)
        except Exception:
            # 降级：直接鼠标移到输入框中心（hover 目标不可用/视口外时）
            box = await inp.bounding_box()
            if box:
                await self._move_mouse(
                    int(box["x"] + box["width"] / 2),
                    int(box["y"] + box["height"] / 2),
                    steps=5,
                )
                await asyncio.sleep(0.2)
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await inp.click(force=True)
        await asyncio.sleep(random.uniform(0.3, 0.8))
        
        # 5.2 逐字输入，10%概率模拟打错字再删掉
        if random.random() < 0.1 and len(question) > 5:
            # 打错一个字
            wrong_char = random.choice('asdfghjkl')
            await inp.type(wrong_char, delay=random.uniform(50, 150))
            await asyncio.sleep(random.uniform(0.2, 0.5))
            # 删掉
            await inp.press('Backspace')
            await asyncio.sleep(random.uniform(0.3, 0.8))
        
        # 正常输入，输入到一半偶尔停顿一下，像真人思考
        await inp.press_sequentially(question[:len(question)//2], delay=self._typing_delay())
        # 20%概率输入到一半停一下
        if random.random() < 0.2:
            await asyncio.sleep(random.uniform(1.0, 2.5))
        await inp.press_sequentially(question[len(question)//2:], delay=self._typing_delay())
        await asyncio.sleep(random.uniform(0.2, 0.5))
        
        # 10%概率点错地方再点回来，更像真人
        if random.random() < 0.1:
            if getattr(self, "HUMANIZE_LEVEL", 0) >= 1:
                # DeepSeek：先贝塞尔移到随机点再点击（避免 click 瞬移）
                tx, ty = random.randint(100, 800), random.randint(100, 400)
                await self._move_mouse(tx, ty, steps=random.randint(15, 35))
                await page.mouse.click()
            else:
                await page.mouse.click(
                    random.randint(100, 800),
                    random.randint(100, 400)
                )
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await inp.click()
            await asyncio.sleep(random.uniform(0.2, 0.5))
        
        # 5.3 输完后20%概率停一下再按回车，像在检查
        if random.random() < 0.2:
            await asyncio.sleep(random.uniform(2.0, 3.5))

        # 6. 记录当前回答块数（定位本次新增）
        try:
            base_count = await page.locator(self.ANSWER_BLOCK_SELECTOR).count()
        except Exception:
            base_count = 0

        # 7. 回车发送（同时监听回答接口的全部响应，直到回答真正结束）
        print("[ask] 步骤7 发送问题", flush=True)
        self.captured_responses.clear()
        answer_url_pattern = getattr(self, 'ANSWER_URL_PATTERN', None)

        if answer_url_pattern:
            # 监听所有匹配"回答接口"的响应（部分模型如文心分"搜索过程流"+"最终回答流"两个响应）
            # 实时填充 self.captured_responses，让 wait_for_answer 的 has_capture 生效（有捕获≤12s返回）
            pattern_sub = answer_url_pattern.replace("**", "")

            async def _on_resp(resp):
                try:
                    if resp.url and pattern_sub in resp.url:
                        body_bytes = await resp.body()
                        try:
                            body = self._fix_double_encoding(body_bytes.decode('utf-8'))
                        except Exception:
                            body = body_bytes.decode('utf-8', errors='replace')
                        self.captured_responses[resp.url] = body
                        print(f"[网络拦截] 实时捕获: {len(body)} 字节", flush=True)
                except Exception:
                    pass

            self.captured_responses.clear()
            page.on("response", _on_resp)
            try:
                await inp.press("Enter")
            except Exception:
                pass
            # 7.5 发送后处理模型特有弹窗（如元宝二次确认选择框），需在等待回答完成前处理
            await self.handle_post_send_popups()
            # 等待回答真正完成（停止按钮消失 + 内容稳定）后再收口
            await self.wait_for_answer(base_count)
            page.remove_listener("response", _on_resp)
            if self.captured_responses:
                # 取最后一个响应（最终回答流；单响应模型即唯一响应）做短响应验证码检查
                last_body = list(self.captured_responses.values())[-1]
                if len(last_body) < 2000 and await self._check_captcha(strict=True):
                    print("[ask] ⚠️ 检测到人工验证（短响应+验证页面）", flush=True)
                    raise CaptchaDetected("人工验证")
            else:
                # 没捕获到任何响应（可能接口路径变化/页面状态异常）。
                # 不再重复回车重试：重试会让总时长超过外层 260s 超时，
                # 外层超时取消 playwright 会卡死（驱动层不响应取消、锁永占）。
                # 直接按 DOM 判定返回，缺口由 fix_runner 补齐。
                print("[网络拦截] 未捕获到回答响应（不重试，缺口由fix_runner补）", flush=True)
                await self.wait_for_answer(base_count)
        else:
            await inp.press("Enter")
            await self.handle_post_send_popups()
            # 无网络拦截模式：仍需等待回答完成
            await self.wait_for_answer(base_count)

        # 8. 等待回答真正完成（已在拦截收口时调用 wait_for_answer）

        # 8.5 回答完成后若页面出现验证也判定（部分验证在回答后才弹出）
        # strict=True：只认 URL/DOM 验证元素，避免正文含"验证码"字样误报
        if await self._check_captcha(strict=True):
            print("[ask] ⚠️ 回答完成后检测到人工验证", flush=True)
            raise CaptchaDetected("人工验证")

        # 9. 提取
        await asyncio.sleep(1)
        answer = await self.extract_answer(base_count)
        citations = await self.extract_citations(base_count)
        search_queries = await self.extract_search_queries(base_count)

        # 10. 回答完后先停留2-5秒，像在看回答内容（保留对话在窗口，便于观察/回溯）
        await asyncio.sleep(random.uniform(2.0, 5.0))
        # 偶尔滚动一下，像在仔细看回答
        if random.random() < 0.3:
            await page.mouse.wheel(0, random.randint(100, 300))
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await page.mouse.wheel(0, -random.randint(100, 300))
            await asyncio.sleep(random.uniform(0.3, 1.0))
        # 不再立即开新会话：保留当前问答在窗口，直到下一次提问时（步骤1）再开新会话
        # 原逻辑回答后立即 new_chat，导致窗口始终是空会话，用户无法看到问答内容

        return {
            "answer": answer["text"],
            "answer_html": answer["html"],
            "citations": citations,
            "search_queries": search_queries,
        }

    async def wait_for_answer(self, base_count: int = 0):
        """等待回答真正完成。

        智能判定：
        - 网络拦截已捕获完整响应（SSE 流结束 = 回答+引用已完整）→ 最多等 12 秒渲染稳定即返回；
        - 未捕获 → 按“停止按钮消失 + 内容长度稳定”的 DOM 判定，最多等 WAIT_TIMEOUT。
        避免旧版“捕获即返回”导致的模型未答完就关窗，也避免 DOM 选择器失效时白白空等。
        """
        page = self.page
        await asyncio.sleep(3)

        has_capture = bool(self.captured_responses)
        max_rounds = 6 if has_capture else self.WAIT_TIMEOUT // 2  # 有捕获≤12s，无捕获≤120s
        # 总时长硬限：操作超时会叠加（count 2s + inner_text 3s + sleep 2s ≈ 7s/轮），
        # 必须按墙钟时间兜底，否则总时长超过外层 260s 超时 → 取消 playwright 会卡死
        start_t = time.time()
        max_elapsed = 10 if has_capture else 118

        last_len = -1
        stable_rounds = 0
        stop_sel = self.stop_button_selector()

        for _ in range(max_rounds):
            if time.time() - start_t > max_elapsed:
                break
            await asyncio.sleep(2)

            has_stop = False
            if stop_sel:
                try:
                    # count() 也带超时：避免页面/驱动异常时 count 挂起导致整轮空转
                    has_stop = await asyncio.wait_for(
                        page.locator(stop_sel).count(), timeout=2) > 0
                except Exception:
                    has_stop = True

            try:
                block = page.locator(self.ANSWER_BLOCK_SELECTOR).last
                cur_len = len(await asyncio.wait_for(block.inner_text(), timeout=3))
            except Exception:
                cur_len = last_len

            if cur_len == last_len:
                stable_rounds += 1
            else:
                stable_rounds = 0
                last_len = cur_len

            # 完成条件A：停止按钮消失且内容非空且稳定
            if not has_stop and last_len > 0 and stable_rounds >= 2:
                await asyncio.sleep(1.5)
                break
            # 完成条件B：内容稳定多轮（停止按钮选择器误匹配也退出）
            if last_len > 0 and stable_rounds >= 3:
                await asyncio.sleep(1.0)
                break
            # 完成条件C：已有网络捕获 + DOM 能拿到内容且稳定
            if has_capture and last_len > 0 and stable_rounds >= 2:
                await asyncio.sleep(1.0)
                break
            # 完成条件D：已有网络捕获但 DOM 拿不到内容（选择器失效），等满短窗口即返回
            if has_capture and stable_rounds >= 4:
                break

        # 无论如何再给引用流/尾部补充一点时间
        await asyncio.sleep(random.uniform(1.0, 2.0))

    async def dismiss_popups(self):
        """自动关闭常见网页弹窗：通知、广告、引导浮层、浏览器提示条。"""
        page = self.page
        
        # 1. 先按常见的×图标关闭按钮选择器找（图标型按钮，没有文字）
        #    每步加短超时保护，避免慢页面/大量匹配导致挂起
        close_selectors = [
            'div[class*="close"]', 'button[class*="close"]',
            'div[class*="Close"]', 'button[class*="Close"]',
            'svg[class*="close"]', '.modal-close', '.popup-close',
        ]
        for sel in close_selectors:
            try:
                btn = page.locator(sel).first
                if await asyncio.wait_for(btn.is_visible(timeout=300), timeout=1.5):
                    await asyncio.wait_for(btn.click(), timeout=2)
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    print(f"[弹窗] 已通过选择器关闭: {sel}", flush=True)
                    break
            except Exception:
                continue
        
        # 2. 再按文字找关闭按钮。
        #    【重要】只用按钮/角色按钮元素匹配，绝不用 get_by_text 全文模糊匹配——
        #    否则"继续/确认/取消/跳过"等常见词会命中上一题回答正文文本，
        #    点击不可交互文本会一直等 actionability 直到超时（元宝高频502根因）。
        #    弹窗特征词在前，通用兜底词放最后。
        close_texts = [
            "暂不", "我知道了", "知道了", "不再提示", "以后再说",
            "下次再说", "暂不开启", "暂不体验", "知道了，继续", "关闭", "×",
            # 通用兜底词（可能出现在正文，务必最后且按钮限定）
            "跳过", "继续", "确认", "取消", "继续提问",
        ]
        for text in close_texts:
            try:
                # 限定按钮类元素：button / [role=button]，避免匹配正文文本
                btn = page.locator(
                    f'button:has-text("{text}"), [role="button"]:has-text("{text}")'
                ).first
                if await asyncio.wait_for(btn.is_visible(timeout=300), timeout=1.5):
                    await asyncio.wait_for(btn.click(), timeout=2)
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    print(f"[弹窗] 已通过文字关闭: {text}", flush=True)
                    break
            except Exception:
                continue
        
        # 3. 关闭Chrome浏览器顶部的提示条（比如"不支持的命令行标记"）
        try:
            # Chrome的infobars关闭按钮
            infobar_close = page.locator('xpath=//div[@role="button" and contains(@class, "close")]').first
            if await infobar_close.is_visible(timeout=200):
                await infobar_close.click()
                await asyncio.sleep(0.3)
                print(f"[弹窗] 已关闭浏览器提示条", flush=True)
        except Exception:
            pass

    def dismiss_chrome_infobar(self):
        """用AppleScript点击Chrome顶部提示条的关闭按钮（系统级坐标）。"""
        x, y = self.WINDOW_POS
        w, h = self.WINDOW_SIZE
        # 提示条高度约40px，关闭×在窗口右上角
        close_x = x + w - 25
        close_y = y + 45
        script = f'''
        tell application "System Events"
            click at {{{close_x}, {close_y}}}
        end tell
        '''
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
            print(f"[提示条] 已点击关闭按钮 ({close_x}, {close_y})", flush=True)
        except Exception as e:
            print(f"[提示条] 关闭失败: {e}", flush=True)
