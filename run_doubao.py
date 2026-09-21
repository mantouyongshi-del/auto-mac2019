"""
豆包问答服务入口（端口 8002）。
"""
import uvicorn

from bots.doubao import DoubaoBrowser
from core.server_base import create_app

browser = DoubaoBrowser()
app = create_app(browser, service_name="laya-doubao")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8002)
