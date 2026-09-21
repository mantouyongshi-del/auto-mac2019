"""元宝入口：端口 8004"""
import uvicorn
from bots.yuanbao import YuanbaoBrowser
from core.server_base import create_app

browser = YuanbaoBrowser()
app = create_app(browser, service_name="laya-yuanbao")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8004, log_level="info")
