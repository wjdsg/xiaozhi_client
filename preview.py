"""只启动网页预览，不初始化麦克风、扬声器或远端语音服务。"""

import os
import sys
from pathlib import Path

from aiohttp import web

PROJECT_DIR = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_DIR / "static"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.utils.page_inject import render_index_html


async def index(_request: web.Request) -> web.Response:
    return web.Response(text=render_index_html(), content_type="text/html")


app = web.Application()
app.router.add_get("/", index)
app.router.add_static("/assets/", STATIC_DIR, show_index=False)


if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8766)
