"""
DeepSeek 问答服务入口（端口 8000）。
架构：core 公共骨架 + bots/deepseek 模型适配。
防风控：每连续成功 50 次提问，强制休息 30 分钟（计数持久化，重启不丢失）。
"""
import os

os.environ.setdefault("ASK_COUNT_RESET", "50")
os.environ.setdefault("ASK_COOLDOWN_SEC", "1800")

import uvicorn

from bots.deepseek import DeepSeekBrowser
from core.server_base import create_app

browser = DeepSeekBrowser()
app = create_app(browser, service_name="deepseek")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
