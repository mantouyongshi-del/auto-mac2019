"""
豆包网页版适配：选择器待登录后探查确认。
通用主流程由 core 基类提供。
"""
import asyncio
import re

from core.browser_base import BrowserBase, PROJECT_ROOT


class DoubaoBrowser(BrowserBase):
    URL = "https://www.doubao.com/"
    PROFILE_DIR = PROJECT_ROOT / "profiles" / "doubao"
    # 选择器待探查后填
    INPUT_SELECTOR = "textarea"  # 待确认
    ANSWER_BLOCK_SELECTOR = ""   # 待确认
    WAIT_TIMEOUT = 120

    async def is_logged_in(self) -> bool:
        try:
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=5000)
            return True
        except Exception:
            return False

    async def check_login_url(self):
        if any(k in self.page.url for k in ["login", "signin", "passport"]):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="豆包未登录")

    async def new_chat(self) -> bool:
        # 待探查：豆包新对话按钮
        return True

    async def maybe_enable_search(self):
        pass

    def stop_button_selector(self) -> str:
        return "button:has-text('停止'), [aria-label*='停止'], [class*='stop']"

    async def extract_answer(self, base_count: int = 0) -> dict:
        try:
            blocks = self.page.locator(self.ANSWER_BLOCK_SELECTOR)
            count = await blocks.count()
            idx = base_count if count > base_count else max(count - 1, 0)
            block = blocks.nth(idx)
            text = await block.inner_text()
            html = await block.inner_html()
            return {"text": text.strip(), "html": html}
        except Exception:
            pass
        body = await self.page.inner_text("body")
        return {"text": body, "html": body}

    async def extract_citations(self, base_count: int = 0) -> list:
        # 待探查：豆包引用结构
        return []
