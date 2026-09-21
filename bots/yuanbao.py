"""
腾讯元宝网页版适配：只写元宝特有的选择器和提取逻辑。
通用主流程由 core 基类提供。
"""
import asyncio
import json
import re

from core.browser_base import BrowserBase, PROJECT_ROOT


class YuanbaoBrowser(BrowserBase):
    URL = "https://yuanbao.tencent.com"
    PROFILE_DIR = PROJECT_ROOT / "profiles" / "yuanbao"
    INPUT_SELECTOR = ".ql-editor[contenteditable='true']"
    ANSWER_BLOCK_SELECTOR = "[class*='markdown']"
    ANSWER_URL_PATTERN = "**/api/chat/**"
    WAIT_TIMEOUT = 120
    # 第二行第二个
    WINDOW_POS = (896, 540)
    WINDOW_SIZE = (896, 580)
    WINDOW_TITLE_KEYWORD = "元宝"

    # ---------- 登录检查 ----------
    async def is_logged_in(self) -> bool:
        try:
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=5000)
            return True
        except Exception:
            return False

    async def check_login_url(self):
        if any(k in self.page.url for k in ["login", "signin", "passport"]):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="元宝未登录，请先打开浏览器登录")

    # ---------- 新对话（独立会话） ----------
    async def new_chat(self) -> bool:
        try:
            for selector in [
                "text=新建对话",
                "text=新对话",
                "[class*='new-chat']",
                "[class*='newChat']",
                "button:has-text('新建')",
            ]:
                try:
                    btn = self.page.locator(selector).first
                    if await btn.count() > 0:
                        await btn.click()
                        await asyncio.sleep(1.5)
                        await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=10000)
                        await asyncio.sleep(0.5)
                        return True
                except:
                    continue
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"[yuanbao new_chat] 失败: {e}", flush=True)
            return False

    # ---------- 联网搜索 ----------
    async def maybe_enable_search(self):
        pass

    # ---------- 发送问题 ----------
    async def send_question(self, question: str) -> bool:
        """元宝是 Quill 编辑器，需要先点击聚焦，再输入，然后按 Enter 发送"""
        try:
            inp = self.page.locator(self.INPUT_SELECTOR).first
            await inp.click()
            await asyncio.sleep(0.5)
            # 清空已有内容
            await inp.fill("")
            await asyncio.sleep(0.2)
            # 逐字输入
            await inp.press_sequentially(question, delay=40)
            await asyncio.sleep(1)
            # 按 Enter 发送
            await inp.press("Enter")
            await asyncio.sleep(1)
            return True
        except Exception as e:
            print(f"[yuanbao send] 失败: {e}", flush=True)
            return False

    # ---------- 停止按钮（等待信号，待精调） ----------
    def stop_button_selector(self) -> str:
        return ("button:has-text('停止'), [aria-label*='停止'], "
                "[class*='stop'], [class*='generating'], [class*='loading']")

    # ---------- 提取回答（网络拦截优先） ----------
    def _parse_sse(self, raw: str) -> tuple[str, list, list]:
        """解析元宝 SSE 流，返回 (回答文本, 引用列表, 搜索关键词列表)。"""
        answer_text = ""
        citations = []
        search_queries = []

        # 元宝 SSE 格式：data:{...}
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

            # 文本增量：deepSearchAgent 类型，contents[].text
            if data.get("type") == "deepSearchAgent":
                contents = data.get("contents") or []
                for c in contents:
                    if not isinstance(c, dict):
                        continue
                    # 回答文本
                    if c.get("type") == "text":
                        text = c.get("text", "")
                        if text:
                            answer_text += text
                    # 搜索关键词：toolCall 且 status=0（正在搜索）
                    elif c.get("type") == "toolCall" and c.get("tcname") == "web_search" and c.get("status") == 0:
                        items = c.get("items") or []
                        for item in items:
                            if isinstance(item, dict):
                                bubbles = item.get("bubbles") or []
                                for bubble in bubbles:
                                    if isinstance(bubble, dict) and bubble.get("text"):
                                        search_queries.append(bubble.get("text", ""))
                    # 引用：toolCall 且 status=2（已搜索完成）
                    elif c.get("type") == "toolCall" and c.get("status") == 2:
                        items = c.get("items") or []
                        for item in items:
                            if isinstance(item, dict):
                                bubbles = item.get("bubbles") or []
                                for bubble in bubbles:
                                    if isinstance(bubble, dict) and bubble.get("link"):
                                        citations.append({
                                            "ref": bubble.get("text", ""),
                                            "url": bubble.get("link", ""),
                                        })

        # 去重
        seen = set()
        unique_citations = []
        for c in citations:
            if c["url"] not in seen:
                seen.add(c["url"])
                unique_citations.append(c)

        # 搜索关键词去重
        unique_queries = list(dict.fromkeys(search_queries))

        return answer_text.strip(), unique_citations, unique_queries

    async def extract_answer(self, base_count: int = 0) -> dict:
        # 优先从网络拦截的 SSE 流提取
        for url, body in self.captured_responses.items():
            if '/api/chat/' in url:
                try:
                    text, citations, search_queries = self._parse_sse(body)
                    if text:
                        self._last_citations = citations
                        self._last_search_queries = search_queries
                        return {"text": text, "html": text}
                except Exception as e:
                    print(f"[yuanbao parse] 异常: {e}", flush=True)

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

    # ---------- 提取引用 ----------
    async def extract_citations(self, base_count: int = 0) -> list:
        # 优先用 extract_answer 已经解析好的
        if hasattr(self, '_last_citations') and self._last_citations:
            return self._last_citations

        # 兜底：DOM 抓取
        citations = []
        try:
            seen = set()
            links = await self.page.locator("a[href^='http']").all()
            for el in links:
                try:
                    href = await el.get_attribute("href")
                    text = (await el.inner_text()).strip()
                    if (href and "tencent.com" not in href and href not in seen):
                        seen.add(href)
                        citations.append({
                            "ref": text or f"-{len(citations)+1}",
                            "url": href.split("#")[0],
                        })
                except Exception:
                    pass
        except Exception as e:
            print(f"[yuanbao cites] 异常: {e}", flush=True)
        return citations

    # ---------- 提取搜索关键词 ----------
    async def extract_search_queries(self, base_count: int = 0) -> list:
        return getattr(self, '_last_search_queries', [])
