"""
豆包网页版适配。
通过拦截 /chat/completion SSE 流提取回答和引用，不依赖 DOM 渲染。
"""
import asyncio
import json
import re

from core.browser_base import BrowserBase, PROJECT_ROOT


class DoubaoBrowser(BrowserBase):
    URL = "https://www.doubao.com/chat/"
    PROFILE_DIR = PROJECT_ROOT / "profiles" / "doubao"
    INPUT_SELECTOR = "[contenteditable='true']"
    ANSWER_BLOCK_SELECTOR = "[class*='markdown']"
    ANSWER_URL_PATTERN = "**/chat/completion**"  # 网络拦截：回答接口 URL 模式
    WAIT_TIMEOUT = 120
    # 第一行第三个
    WINDOW_POS = (1195, 0)
    WINDOW_SIZE = (597, 540)
    WINDOW_TITLE_KEYWORD = "豆包"

    # ---------- 网络拦截 ----------
    def should_capture_url(self, url: str) -> bool:
        return '/chat/completion' in url

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
        try:
            # 直接跳转到首页 = 新对话，不用点侧边栏按钮（小窗口侧边栏收起）
            await self.page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            # 等输入框就绪
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=10000)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"[doubao new_chat] 失败: {e}", flush=True)
            return False

    async def maybe_enable_search(self):
        pass

    def stop_button_selector(self) -> str:
        return "button:has-text('停止'), [aria-label*='停止'], [class*='stop']"

    # ---------- SSE 解析 ----------
    def _parse_sse(self, raw: str) -> tuple[str, list, list]:
        """解析豆包 SSE 流，返回 (完整回答文本, 引用列表, 搜索关键词列表)。"""
        answer_text = ""
        citations = []
        search_queries = []

        # 按空行分隔事件
        blocks = raw.strip().split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            event_type = None
            data_str = None
            for line in lines:
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()

            if not data_str:
                continue

            try:
                data = json.loads(data_str)
            except Exception:
                continue

            # CHUNK_DELTA：增量回答文本
            if event_type == "CHUNK_DELTA" and "text" in data:
                answer_text += data["text"]

            # STREAM_CHUNK：可能包含引用结果
            if event_type == "STREAM_CHUNK":
                patch_ops = data.get("patch_op", [])
                for op in patch_ops:
                    patch_value = op.get("patch_value", {})
                    content_blocks = patch_value.get("content_block", [])
                    for cb in content_blocks:
                        # block_type 10025 = 搜索结果块（引用）
                        if cb.get("block_type") == 10025:
                            sqr = cb.get("content", {}).get("search_query_result_block", {})
                            # 提取搜索关键词
                            for q in sqr.get("queries", []):
                                search_queries.append(q)
                            # 提取引用
                            for result in sqr.get("results", []):
                                tc = result.get("text_card", {})
                                if tc.get("url"):
                                    citations.append({
                                        "ref": tc.get("title", ""),
                                        "url": tc["url"],
                                    })

        # 搜索关键词去重
        unique_queries = list(dict.fromkeys(search_queries))

        return answer_text, citations, unique_queries

    async def extract_answer(self, base_count: int = 0) -> dict:
        # 优先从网络拦截的 SSE 流提取
        for url, body in self.captured_responses.items():
            if '/chat/completion' in url:
                text, citations, search_queries = self._parse_sse(body)
                if text:
                    # 把引用和搜索关键词存起来，extract_citations 直接用
                    self._last_citations = citations
                    self._last_search_queries = search_queries
                    return {"text": text.strip(), "html": text.strip()}

        # 兜底：DOM 提取
        try:
            blocks = self.page.locator(self.ANSWER_BLOCK_SELECTOR)
            count = await blocks.count()
            if count > 0:
                block = blocks.last
                text = await block.inner_text()
                html = await block.inner_html()
                return {"text": text.strip(), "html": html}
        except Exception:
            pass
        body_text = await self.page.inner_text("body")
        return {"text": body_text, "html": body_text}

    async def extract_citations(self, base_count: int = 0) -> list:
        # 优先用 extract_answer 已经解析好的
        if hasattr(self, '_last_citations') and self._last_citations:
            return self._last_citations

        # 兜底：DOM 抓取
        citations = []
        try:
            try:
                ref_bar = self.page.locator("text=/参考 \\d+ 篇资料/").first
                if await ref_bar.count() > 0:
                    await ref_bar.click()
                    await asyncio.sleep(2)
            except Exception:
                pass
            links = await self.page.locator("a[href^='http']").all()
            seen = set()
            for el in links:
                try:
                    href = await el.get_attribute("href")
                    text = (await el.inner_text()).strip()
                    if (href and "doubao.com" not in href and "bytedance" not in href
                            and "volces.com" not in href and href not in seen):
                        seen.add(href)
                        citations.append({"ref": text or f"-{len(citations)+1}", "url": href.split("#")[0]})
                except:
                    pass
        except Exception:
            pass
        return citations

    # ---------- 提取搜索关键词 ----------
    async def extract_search_queries(self, base_count: int = 0) -> list:
        return getattr(self, '_last_search_queries', [])
