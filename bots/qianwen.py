"""
通义千问网页版适配：只写千问特有的选择器和提取逻辑。
通用主流程由 core 基类提供。
"""
import asyncio
import json
import re

from core.browser_base import BrowserBase, PROJECT_ROOT


class QianwenBrowser(BrowserBase):
    URL = "https://www.qianwen.com/"
    PROFILE_DIR = PROJECT_ROOT / "profiles" / "qianwen"
    INPUT_SELECTOR = "[contenteditable='true']"
    ANSWER_BLOCK_SELECTOR = "[class*='markdown']"
    ANSWER_URL_PATTERN = "**/api/v2/chat**"
    WAIT_TIMEOUT = 120
    # 第一行中间
    WINDOW_POS = (597, 0)
    WINDOW_SIZE = (598, 540)
    WINDOW_TITLE_KEYWORD = "千问"

    # ---------- 登录检查 ----------
    async def is_logged_in(self) -> bool:
        try:
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=5000)
            return True
        except Exception:
            return False

    async def check_login_url(self):
        # 千问未登录会跳登录页，URL 含 login/signin
        if any(k in self.page.url for k in ["login", "signin", "passport"]):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="千问未登录，请先打开浏览器登录")

    # ---------- 新对话（独立会话） ----------
    async def new_chat(self) -> bool:
        try:
            # 直接跳转到首页 = 新对话，不用点侧边栏按钮（小窗口侧边栏收起）
            await self.page.goto("https://www.qianwen.com/", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            # 千问改版：首页可能默认进入"工作"模式(workbench/agent)，提问走 agent 接口，
            # 与 /api/v2/chat 拦截失配。检测到 workbench 时依次尝试切回"日常"对话。
            if "workbench" in self.page.url:
                print("[qianwen new_chat] 检测到workbench，尝试切日常", flush=True)
                for name, fn in (("playwright", self._click_daily_tab_pw),
                                 ("js", self._click_daily_tab_js)):
                    try:
                        await fn()
                    except Exception as e:
                        print(f"[qianwen new_chat] 切日常({name})失败: {e}", flush=True)
                    await asyncio.sleep(2)
                    if "workbench" not in self.page.url:
                        print(f"[qianwen new_chat] 切日常({name})成功", flush=True)
                        break
            # 兜底：直接访问 /chat/ 对话路径
            if "workbench" in self.page.url:
                print("[qianwen new_chat] 尝试 /chat/ 路径", flush=True)
                try:
                    await self.page.goto("https://www.qianwen.com/chat/",
                                         wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"[qianwen new_chat] /chat/ 失败: {e}", flush=True)
            # 仍在工作台则放弃（避免在错误页面提问空等120秒）
            if "workbench" in self.page.url:
                print("[qianwen new_chat] 仍在workbench，放弃本次新会话", flush=True)
                return False
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=10000)
            await asyncio.sleep(0.5)
            print(f"[qianwen new_chat] 完成 URL={self.page.url}", flush=True)
            return True
        except Exception as e:
            print(f"[qianwen new_chat] 失败: {e}", flush=True)
            return False

    async def _click_daily_tab_pw(self):
        """playwright 文本定位点击「日常」对话模式 Tab。"""
        els = await self.page.get_by_text("日常", exact=True).all()
        if not els:
            raise RuntimeError("未找到「日常」文本")
        await els[-1].click(timeout=5000)

    async def _click_daily_tab_js(self):
        """JS 兜底：点击最后一个文本恰为「日常」的叶子元素。"""
        ok = await self.page.evaluate("""
          (() => {
            const els = [...document.querySelectorAll('*')].filter(
              el => el.children.length === 0 && el.textContent.trim() === '日常'
            );
            if (!els.length) return false;
            els[els.length - 1].click();
            return true;
          })()
        """)
        if not ok:
            raise RuntimeError("JS 未找到「日常」元素")

    # ---------- 联网搜索（千问默认联网，暂不特殊处理） ----------
    async def maybe_enable_search(self):
        pass

    # ---------- 停止按钮（等待信号，待精调） ----------
    def stop_button_selector(self) -> str:
        return ("button:has-text('停止'), [aria-label*='停止'], "
                "[class*='stop'], [class*='generating']")

    # ---------- 提取回答 ----------
    def _parse_sse(self, raw: str) -> tuple[str, list]:
        """解析千问 SSE 流，返回 (回答文本, 引用列表)。"""
        last_answer = ""
        citations = []

        # 千问 SSE 格式：data:{...}\n\n
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

            messages = data.get("data", {}).get("messages", [])
            for msg in messages:
                mime = msg.get("mime_type", "")
                content = msg.get("content", "")

                # 回答文本：全量替换模式，取最后一个非空 content
                if content and isinstance(content, str) and len(content) > 50:
                    if mime == "text" or "text" in mime:
                        last_answer = content

                # 引用：从 meta_data.multi_load[].content.docs[] 里提取
                meta = msg.get("meta_data", {})
                if isinstance(meta, dict):
                    multi_load = meta.get("multi_load", [])
                    if isinstance(multi_load, list):
                        for ml_item in multi_load:
                            if isinstance(ml_item, dict):
                                content = ml_item.get("content", {})
                                if isinstance(content, dict):
                                    docs = content.get("docs", [])
                                    if isinstance(docs, list):
                                        for doc in docs:
                                            if isinstance(doc, dict) and doc.get("url"):
                                                citations.append({
                                                    "ref": doc.get("title", doc.get("name", "")),
                                                    "url": doc.get("raw_url", doc.get("url", "")),
                                                })

        # 去重引用
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
            if '/api/v2/chat' in url:
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

    # ---------- 提取引用（千问"来源"区域） ----------
    async def extract_citations(self, base_count: int = 0) -> list:
        # 优先从网络拦截的 SSE 流提取
        for url, body in self.captured_responses.items():
            if '/api/v2/chat' in url:
                _, citations = self._parse_sse(body)
                if citations:
                    return citations

        # 兜底：DOM 抓取
        citations = []
        try:
            # 等"来源"面板渲染（回答文本稳定后，来源卡片可能还没出来）
            try:
                await self.page.wait_for_selector("a[href^='http']", timeout=8000)
                await asyncio.sleep(1)
            except Exception:
                pass

            # 千问来源是标签式卡片，直接抓页面所有外链，排除自家域名
            seen = set()
            links = await self.page.locator("a[href^='http']").all()
            for el in links:
                try:
                    href = await el.get_attribute("href")
                    text = (await el.inner_text()).strip()
                    if (href and "qianwen.com" not in href
                            and "aliyun.com" not in href and href not in seen):
                        seen.add(href)
                        citations.append({
                            "ref": text or f"-{len(citations)+1}",
                            "url": href.split("#")[0],
                        })
                except Exception:
                    pass
        except Exception as e:
            print(f"[qianwen cites] 异常: {e}", flush=True)
        return citations
