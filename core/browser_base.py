"""
浏览器自动化基类：所有模型（DeepSeek/千问/豆包...）共享的通用骨架。

子类只需配置类属性 + 实现模型特有的选择器和提取逻辑，
主流程（新会话→输入→发送→等待→提取）由本类通用完成。
"""
import asyncio
import random
from pathlib import Path
from playwright.async_api import async_playwright
from fastapi import HTTPException

# 项目根目录（所有模型共用 profile / 日志的根）
PROJECT_ROOT = Path("/Users/alili/laya")
LOG_DIR = PROJECT_ROOT / "server_logs"


class BrowserBase:
    # ---------- 子类必须配置的类属性 ----------
    URL = ""                              # 目标网页
    PROFILE_DIR = ""                      # Chrome 持久化 profile 绝对路径
    INPUT_SELECTOR = "textarea"           # 输入框选择器
    ANSWER_BLOCK_SELECTOR = ""            # 回答正文块选择器
    WAIT_TIMEOUT = 120                     # 等待回答最长秒数
    HEADLESS = False                      # 首次登录需 False 看到浏览器

    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None

    # ---------- 通用：启动 / 关闭 ----------
    async def start(self):
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.PROFILE_DIR),
            channel="chrome",
            headless=self.HEADLESS,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                # 禁用"恢复之前的页面"崩溃提示气泡
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
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
        await asyncio.sleep(2)

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

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

    # ---------- 子类可覆盖（有默认值） ----------
    def stop_button_selector(self) -> str:
        """停止生成按钮选择器（存在=仍在生成）。返回空串表示不用此信号。"""
        return ""

    async def maybe_enable_search(self):
        """若该模型有"联网搜索"开关，在此开启。默认什么都不做。"""
        pass

    async def check_login_url(self):
        """发送前检查登录态（URL 跳转登录页则抛 401）。默认不检查。"""
        return

    # ---------- 通用：提问主流程 ----------
    async def ask(self, question: str) -> dict:
        page = self.page

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
        await inp.click()
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await inp.press_sequentially(question, delay=random.uniform(50, 150))
        await asyncio.sleep(random.uniform(0.2, 0.5))

        # 6. 记录当前回答块数（定位本次新增）
        try:
            base_count = await page.locator(self.ANSWER_BLOCK_SELECTOR).count()
        except Exception:
            base_count = 0

        # 7. 回车发送
        await inp.press("Enter")

        # 8. 等待回答完成
        await self.wait_for_answer(base_count)

        # 9. 提取
        await asyncio.sleep(1)
        answer = await self.extract_answer(base_count)
        citations = await self.extract_citations(base_count)

        return {
            "answer": answer["text"],
            "answer_html": answer["html"],
            "citations": citations,
        }

    async def wait_for_answer(self, base_count: int = 0):
        """多信号等待回答完成：停止按钮存在性 + 内容长度稳定性。"""
        page = self.page
        await asyncio.sleep(3)  # 等流式开始

        last_len = -1
        stable_rounds = 0
        stop_sel = self.stop_button_selector()

        for _ in range(self.WAIT_TIMEOUT // 2):
            await asyncio.sleep(2)

            # 信号A：停止按钮
            has_stop = False
            if stop_sel:
                try:
                    has_stop = await page.locator(stop_sel).count() > 0
                except Exception:
                    has_stop = True  # 检测异常保守按"仍在生成"

            # 信号B：回答块内容长度
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

            # 完成条件1：无停止按钮 + 内容已出现 + 连续2轮稳定
            if not has_stop and last_len > 0 and stable_rounds >= 2:
                await asyncio.sleep(1.5)
                break
            # 完成条件2：兜底——内容已出现且连续5轮稳定
            if last_len > 0 and stable_rounds >= 5:
                await asyncio.sleep(1.0)
                break
