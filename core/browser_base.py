"""
浏览器自动化基类：所有模型（DeepSeek/千问/豆包...）共享的通用骨架。

子类只需配置类属性 + 实现模型特有的选择器和提取逻辑，
主流程（新会话→输入→发送→等待→提取）由本类通用完成。
"""
import asyncio
import random
import subprocess
from pathlib import Path
from playwright.async_api import async_playwright
from fastapi import HTTPException

# 项目根目录（所有模型共用 profile / 日志的根）
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
LOG_DIR = PROJECT_ROOT / "server_logs"


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

    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self.captured_responses = {}  # URL -> body text（网络拦截捕获）

    # ---------- 通用：启动 / 关闭 ----------
    async def start(self):
        # 启动前清理Chrome锁文件，避免异常退出后下次启动失败
        lock_files = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
        for f in lock_files:
            lock_path = Path(self.PROFILE_DIR) / f
            if lock_path.exists():
                try:
                    lock_path.unlink()
                    print(f"[启动] 清理锁文件: {f}", flush=True)
                except Exception:
                    pass
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.PROFILE_DIR),
            channel="chrome",
            headless=self.HEADLESS,
            viewport={"width": self.WINDOW_SIZE[0], "height": self.WINDOW_SIZE[1]},
            args=[
                "--disable-blink-features=AutomationControlled",
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

    # ---------- 子类必须实现 ----------
    async def new_chat(self) -> bool:
        """开启新对话，确保每题独立会话。"""
        raise NotImplementedError

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

    # ---------- 子类可覆盖（有默认值） ----------
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

    # ---------- 通用：提问主流程 ----------
    async def ask(self, question: str) -> dict:
        page = self.page

        # 0. 先关掉可能弹出的广告/通知弹窗
        await self.dismiss_popups()

        # 1. 每题独立会话
        if not await self.new_chat():
            raise RuntimeError("开启新会话失败")

        # 2. 模拟人类：随机停顿
        await asyncio.sleep(random.uniform(0.5, 2.0))

        # 3. 登录检查
        await self.check_login_url()

        # 4. 开启联网搜索（模型特有）
        await self.maybe_enable_search()

        # 5. 逐字输入
        inp = page.locator(self.INPUT_SELECTOR).first
        
        # 5.1 模拟真人鼠标移动：先随机移到页面某点，再慢慢移到输入框
        await page.mouse.move(
            random.randint(100, 800),
            random.randint(100, 400),
            steps=random.randint(10, 30)
        )
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await inp.hover(force=True)
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await inp.click()
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
        await inp.press_sequentially(question[:len(question)//2], delay=random.uniform(50, 150))
        # 20%概率输入到一半停一下
        if random.random() < 0.2:
            await asyncio.sleep(random.uniform(1.0, 2.5))
        await inp.press_sequentially(question[len(question)//2:], delay=random.uniform(50, 150))
        await asyncio.sleep(random.uniform(0.2, 0.5))
        
        # 10%概率点错地方再点回来，更像真人
        if random.random() < 0.1:
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

        # 7. 回车发送（同时监听回答接口响应）
        self.captured_responses.clear()

        # 如果子类需要网络拦截，用 expect_response 精准捕获
        answer_url_pattern = getattr(self, 'ANSWER_URL_PATTERN', None)
        if answer_url_pattern:
            try:
                async with page.expect_response(answer_url_pattern, timeout=self.WAIT_TIMEOUT * 1000) as resp_info:
                    await inp.press("Enter")
                resp = await resp_info.value
                body_bytes = await resp.body()
                # 修复双重编码
                try:
                    body = self._fix_double_encoding(body_bytes.decode('utf-8'))
                except Exception:
                    body = body_bytes.decode('utf-8', errors='replace')
                self.captured_responses[resp.url] = body
                print(f"[网络拦截] 成功捕获: {len(body)} 字节", flush=True)
            except Exception as e:
                print(f"[网络拦截] 失败: {e}", flush=True)
                await inp.press("Enter")
        else:
            await inp.press("Enter")

        # 8. 等待回答完成
        await self.wait_for_answer(base_count)

        # 9. 提取
        await asyncio.sleep(1)
        answer = await self.extract_answer(base_count)
        citations = await self.extract_citations(base_count)
        search_queries = await self.extract_search_queries(base_count)

        # 10. 回答完后先停留2-5秒，像在看回答内容，再开新对话
        await asyncio.sleep(random.uniform(2.0, 5.0))
        # 偶尔滚动一下，像在仔细看回答
        if random.random() < 0.3:
            await page.mouse.wheel(0, random.randint(100, 300))
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await page.mouse.wheel(0, -random.randint(100, 300))
            await asyncio.sleep(random.uniform(0.3, 1.0))
        
        try:
            await self.new_chat()
        except Exception:
            pass

        return {
            "answer": answer["text"],
            "answer_html": answer["html"],
            "citations": citations,
            "search_queries": search_queries,
        }

    async def wait_for_answer(self, base_count: int = 0):
        """等待回答完成。如果已通过 expect_response 捕获到回答，直接返回。"""
        if self.captured_responses:
            await asyncio.sleep(1)  # 等 SSE 流完全结束
            return

        page = self.page
        await asyncio.sleep(3)

        last_len = -1
        stable_rounds = 0
        stop_sel = self.stop_button_selector()

        for _ in range(self.WAIT_TIMEOUT // 2):
            await asyncio.sleep(2)

            has_stop = False
            if stop_sel:
                try:
                    has_stop = await page.locator(stop_sel).count() > 0
                except Exception:
                    has_stop = True

            try:
                block = page.locator(self.ANSWER_BLOCK_SELECTOR).last
                cur_len = len(await block.inner_text())
            except Exception:
                cur_len = last_len

            if cur_len == last_len:
                stable_rounds += 1
            else:
                stable_rounds = 0
                last_len = cur_len

            if not has_stop and last_len > 0 and stable_rounds >= 2:
                await asyncio.sleep(1.5)
                break
            if last_len > 0 and stable_rounds >= 5:
                await asyncio.sleep(1.0)
                break

    async def dismiss_popups(self):
        """自动关闭常见网页弹窗：通知、广告、引导浮层、浏览器提示条。"""
        page = self.page
        
        # 1. 先按常见的×图标关闭按钮选择器找（图标型按钮，没有文字）
        close_selectors = [
            'div[class*="close"]', 'button[class*="close"]',
            'div[class*="Close"]', 'button[class*="Close"]',
            'svg[class*="close"]', '.modal-close', '.popup-close',
        ]
        for sel in close_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=200):
                    await btn.click()
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    print(f"[弹窗] 已通过选择器关闭: {sel}", flush=True)
                    break
            except Exception:
                continue
        
        # 2. 再按文字找关闭按钮
        close_texts = [
            "暂不", "关闭", "×", "我知道了", "知道了", "不再提示",
            "取消", "以后再说", "下次再说", "暂不开启", "拒绝", "暂不体验"
        ]
        for text in close_texts:
            try:
                btn = page.get_by_text(text, exact=False).first
                if await btn.is_visible(timeout=200):
                    await btn.click()
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
