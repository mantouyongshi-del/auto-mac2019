"""文心一言入口：端口 8003"""
import uvicorn
from bots.wenxin import WenxinBrowser
from core.server_base import create_app

browser = WenxinBrowser()
app = create_app(browser, service_name="laya-wenxin")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="info")
