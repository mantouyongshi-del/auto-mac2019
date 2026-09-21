"""
通义千问网页版适配：只写千问特有的选择器和提取逻辑。
通用主流程由 core 基类提供。
"""
import asyncio
import re

from core.browser_base import BrowserBase, PROJECT_ROOT


class QianwenBrowser(BrowserBase):
    URL = "https://www.qianwen.com/"
    PROFILE_DIR = PROJECT_ROOT / "profiles" / "qianwen"
    INPUT_SELECTOR = "[contenteditable='true']"
    ANSWER_BLOCK_SELECTOR = "[class*='markdown']"
    WAIT_TIMEOUT = 120

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
            btn = self.page.locator("text=新建对话").first
            await btn.click()
            await asyncio.sleep(1.5)
            # 等输入框就绪
            await self.page.wait_for_selector(self.INPUT_SELECTOR, timeout=10000)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"[qianwen new_chat] 失败: {e}", flush=True)
            return False

    # ---------- 联网搜索（千问默认联网，暂不特殊处理） ----------
    async def maybe_enable_search(self):
        pass

    # ---------- 停止按钮（等待信号，待精调） ----------
    def stop_button_selector(self) -> str:
        return ("button:has-text('停止'), [aria-label*='停止'], "
                "[class*='stop'], [class*='generating']")

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

    # ---------- 提取引用（千问"来源"区域） ----------
    async def extract_citations(self, base_count: int = 0) -> list:
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
