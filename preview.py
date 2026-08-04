"""只启动网页预览，不初始化麦克风、扬声器或远端语音服务。"""

from pathlib import Path

from aiohttp import web


PROJECT_DIR = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_DIR / "static"


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


app = web.Application()
app.router.add_get("/", index)
app.router.add_static("/assets/", STATIC_DIR, show_index=False)


if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8766)
