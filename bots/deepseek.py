"""
DeepSeek 网页版适配：只写 DeepSeek 特有的选择器和提取逻辑。
通用主流程（启动/输入/等待/HTTP 接口）由 core 基类提供。
"""
import asyncio
import re
from fastapi import HTTPException

from core.browser_base import BrowserBase, PROJECT_ROOT


class DeepSeekBrowser(BrowserBase):
    URL = "https://chat.deepseek.com"
    PROFILE_DIR = PROJECT_ROOT / "profiles" / "deepseek"
    INPUT_SELECTOR = "textarea"
    ANSWER_BLOCK_SELECTOR = ".ds-assistant-message-main-content"
    WAIT_TIMEOUT = 120

    # ---------- 登录检查 ----------
    async def is_logged_in(self) -> bool:
        try:
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=5000)
            return True
        except Exception:
            return False

    async def check_login_url(self):
        if "sign_in" in self.page.url:
            raise HTTPException(status_code=401, detail="DeepSeek 未登录，请先打开浏览器登录")

    # ---------- 新对话（独立会话） ----------
    async def new_chat(self) -> bool:
        try:
            btn = self.page.locator("text=开启新对话").first
            await btn.click()
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=10000)
            await asyncio.sleep(1)
            return True
        except Exception as e:
            print(f"[new_chat] 失败: {e}", flush=True)
            return False

    # ---------- 开启联网智能搜索 ----------
    async def maybe_enable_search(self):
        try:
            search_btn = self.page.locator("text=智能搜索")
            if await search_btn.count() > 0:
                cls = await search_btn.evaluate("el => el.closest('button,div')?.className || ''")
                if "selected" not in cls:
                    await search_btn.click()
                    await asyncio.sleep(0.5)
        except Exception:
            pass

    # ---------- 停止按钮（等待信号） ----------
    def stop_button_selector(self) -> str:
        return ("button:has-text('停止'), [aria-label='停止'], "
                "[aria-label*='stop'], [class*='stop-generation'], [class*='stopGen']")

    # ---------- 提取回答 ----------
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

    # ---------- 提取引用 ----------
    async def extract_citations(self, base_count: int = 0) -> list:
        citations = []
        try:
            blocks = self.page.locator(self.ANSWER_BLOCK_SELECTOR)
            count = await blocks.count()
            idx = base_count if count > base_count else max(count - 1, 0)
            block = blocks.nth(idx)

            # 1) 带链接的引用
            links = block.locator("a[href^='http']")
            lcount = await links.count()
            seen = set()
            for i in range(lcount):
                el = links.nth(i)
                href = await el.get_attribute("href")
                text = (await el.inner_text()).strip()
                if href and "deepseek.com" not in href and href not in seen:
                    seen.add(href)
                    citations.append({"ref": text, "url": href.split("#")[0]})

            # 2) 引用标记编号，补全无外链项
            have_nums = set()
            for c in citations:
                m = re.search(r'(\d+)', c["ref"])
                if m:
                    have_nums.add(int(m.group(1)))
            markers = block.locator(".ds-markdown-cite, sup")
            mcount = await markers.count()
            all_nums = set()
            for i in range(min(mcount, 600)):
                el = markers.nth(i)
                t = (await el.inner_text()).strip()
                m = re.fullmatch(r'-?\s*(\d{1,3})', t)
                if m:
                    all_nums.add(int(m.group(1)))
            for num in sorted(all_nums - have_nums):
                citations.append({"ref": f"-{num}", "url": None})
        except Exception:
            pass
        return citations
