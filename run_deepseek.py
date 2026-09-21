"""
DeepSeek 问答服务入口（端口 8000）。
架构：core 公共骨架 + bots/deepseek 模型适配。
"""
import uvicorn

from bots.deepseek import DeepSeekBrowser
from core.server_base import create_app

browser = DeepSeekBrowser()
app = create_app(browser, service_name="laya-deepseek")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
