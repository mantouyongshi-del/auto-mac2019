"""
DeepSeek 网页版适配。
通过拦截 /api/v0/chat/completion SSE 流提取回答和引用。
"""
import asyncio
import json
import re
from fastapi import HTTPException

from core.browser_base import BrowserBase, PROJECT_ROOT


class DeepSeekBrowser(BrowserBase):
    URL = "https://chat.deepseek.com"
    PROFILE_DIR = PROJECT_ROOT / "profiles" / "deepseek"
    INPUT_SELECTOR = "textarea"
    ANSWER_BLOCK_SELECTOR = ".ds-assistant-message-main-content"
    ANSWER_URL_PATTERN = "**/api/v0/chat/completion**"
    WAIT_TIMEOUT = 120
    # 第一行第一个
    WINDOW_POS = (0, 0)
    WINDOW_SIZE = (597, 540)
    WINDOW_TITLE_KEYWORD = "DeepSeek"

    async def is_logged_in(self) -> bool:
        try:
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=5000)
            return True
        except Exception:
            return False

    async def check_login_url(self):
        if "sign_in" in self.page.url:
            raise HTTPException(status_code=401, detail="DeepSeek 未登录，请先打开浏览器登录")

    async def new_chat(self) -> bool:
        try:
            # 直接跳转到首页 = 新对话，不用点侧边栏按钮（小窗口侧边栏收起）
            await self.page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            # 等输入框就绪
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=10000)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"[new_chat] 失败: {e}", flush=True)
            return False

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

    def stop_button_selector(self) -> str:
        return ("button:has-text('停止'), [aria-label='停止'], "
                "[aria-label*='stop'], [class*='stop-generation'], [class*='stopGen']")

    # ---------- SSE 解析 ----------
    def _parse_sse(self, raw: str) -> tuple[str, list]:
        """解析 DeepSeek SSE 流，返回 (回答文本, 引用列表)。"""
        answer_text = ""
        citations = []
        all_text_chunks = []

        blocks = raw.strip().split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            data_str = None
            for line in lines:
                if line.startswith("data:"):
                    data_str = line[5:].strip()
            if not data_str:
                continue
            try:
                data = json.loads(data_str)
            except Exception:
                continue

            # 提取引用（搜索结果）
            if isinstance(data.get("v"), list) and data.get("p", "").endswith("/results"):
                for result in data["v"]:
                    if isinstance(result, dict) and result.get("url"):
                        citations.append({"ref": result.get("title", ""), "url": result["url"]})

            # 提取文本增量
            # DeepSeek 有两种增量：
            # 1. 裸文本：{"v": "带回"}（没有 p 字段）
            # 2. 路径文本：{"p": "response/fragments/-1/content", "o": "APPEND", "v": "6"}
            if isinstance(data.get("v"), str):
                if "p" not in data or "/content" in data.get("p", ""):
                    all_text_chunks.append(data["v"])

        answer_text = "".join(all_text_chunks)

        # 去重
        seen = set()
        unique_citations = []
        for c in citations:
            if c["url"] not in seen:
                seen.add(c["url"])
                unique_citations.append(c)

        return answer_text.strip(), unique_citations

    async def extract_answer(self, base_count: int = 0) -> dict:
        # 优先从网络拦截的 SSE 流提取
        for url, body in self.captured_responses.items():
            if '/chat/completion' in url:
                text, citations = self._parse_sse(body)
                if text:
                    self._last_citations = citations
                    return {"text": text.strip(), "html": text.strip()}

        # 兜底：DOM 提取
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
        # 优先从网络拦截的 SSE 流提取
        for url, body in self.captured_responses.items():
            if '/chat/completion' in url:
                _, citations = self._parse_sse(body)
                if citations:
                    return citations

        # 兜底：DOM 抓取
        citations = []
        try:
            blocks = self.page.locator(self.ANSWER_BLOCK_SELECTOR)
            count = await blocks.count()
            idx = base_count if count > base_count else max(count - 1, 0)
            block = blocks.nth(idx)
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
        except Exception:
            pass
        return citations
