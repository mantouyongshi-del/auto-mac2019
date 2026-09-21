"""
文心一言网页版适配：只写文心一言特有的选择器和提取逻辑。
通用主流程由 core 基类提供。
"""
import asyncio
import json
import re

from core.browser_base import BrowserBase, PROJECT_ROOT


class WenxinBrowser(BrowserBase):
    URL = "https://yiyan.baidu.com"
    PROFILE_DIR = PROJECT_ROOT / "profiles" / "wenxin"
    INPUT_SELECTOR = "textarea"
    ANSWER_BLOCK_SELECTOR = "[class*='markdown']"
    ANSWER_URL_PATTERN = "**/aichat/api/conversation**"
    WAIT_TIMEOUT = 120
    # 第二行第一个
    WINDOW_POS = (0, 540)
    WINDOW_SIZE = (896, 580)
    WINDOW_TITLE_KEYWORD = "文心"

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
            raise HTTPException(status_code=401, detail="文心一言未登录，请先打开浏览器登录")

    # ---------- 新对话（独立会话） ----------
    async def new_chat(self) -> bool:
        try:
            # 尝试多种方式找新建对话按钮
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
            # 没找到就直接用当前页面
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"[wenxin new_chat] 失败: {e}", flush=True)
            return False

    # ---------- 联网搜索 ----------
    async def maybe_enable_search(self):
        pass

    # ---------- 停止按钮 ----------
    def stop_button_selector(self) -> str:
        return ("button:has-text('停止'), [aria-label*='停止'], "
                "[class*='stop'], [class*='generating']")

    # ---------- 提取回答（网络拦截优先） ----------
    def _parse_sse(self, raw: str) -> tuple[str, list]:
        """解析文心一言 SSE 流，返回 (回答文本, 引用列表)。"""
        last_answer = ""
        citations = []

        # 文心一言 SSE 格式：event:message\ndata:{...}
        blocks = raw.strip().split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            data_str = None
            event_type = None
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

            gen = data.get("data", {}).get("message", {}).get("content", {}).get("generator", {})
            comp = gen.get("component", "")
            text = gen.get("text", "")

            # 回答文本：markdown-yiyan 组件
            if comp == "markdown-yiyan" and text:
                last_answer = text

            # 引用：data.referenceList 数组
            gen_data = gen.get("data", {})
            if isinstance(gen_data, dict):
                ref_list = gen_data.get("referenceList", [])
                if isinstance(ref_list, list):
                    for ref in ref_list:
                        if isinstance(ref, dict) and ref.get("url"):
                            citations.append({
                                "ref": ref.get("text", ref.get("title", "")),
                                "url": ref.get("url", ""),
                            })

        # 去重
        seen = set()
        unique_citations = []
        for c in citations:
            if c["url"] not in seen:
                seen.add(c["url"])
                unique_citations.append(c)

        return last_answer.strip(), unique_citations

    async def extract_answer(self, base_count: int = 0) -> dict:
        # 优先从网络拦截的 SSE 流提取
        for url, body in self.captured_responses.items():
            if '/aichat/api/conversation' in url:
                text, citations = self._parse_sse(body)
                if text:
                    self._last_citations = citations
                    return {"text": text, "html": text}

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
        # 优先从网络拦截的 SSE 流提取
        for url, body in self.captured_responses.items():
            if self.ANSWER_URL_PATTERN.replace("**", "") in url:
                _, citations = self._parse_sse(body)
                if citations:
                    return citations

        # 兜底：DOM 抓取
        citations = []
        try:
            seen = set()
            links = await self.page.locator("a[href^='http']").all()
            for el in links:
                try:
                    href = await el.get_attribute("href")
                    text = (await el.inner_text()).strip()
                    if (href and "baidu.com" not in href and href not in seen):
                        seen.add(href)
                        citations.append({
                            "ref": text or f"-{len(citations)+1}",
                            "url": href.split("#")[0],
                        })
                except Exception:
                    pass
        except Exception as e:
            print(f"[wenxin cites] 异常: {e}", flush=True)
        return citations
