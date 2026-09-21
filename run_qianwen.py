"""
千问问答服务入口（端口 8001）。
架构：core 公共骨架 + bots/qianwen 模型适配。
"""
import uvicorn

from bots.qianwen import QianwenBrowser
from core.server_base import create_app

browser = QianwenBrowser()
app = create_app(browser, service_name="laya-qianwen")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
