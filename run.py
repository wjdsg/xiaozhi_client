# Author: mjw
# Date: 2026-07-09
import asyncio, webbrowser, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import WebBridge

CONFIG = {
    "xz_url":    "ws://10.150.101.33:5000/xiaozhi/v1/",
    "xz_token":  "test-token",
    "did":       "2c:db:07:09:da:31",
    "cid":       "cff1e306-cce9-4ff6-b33e-60fefabf32c4",
    "host":      "127.0.0.1",
    "port":      8765,
}

async def main():
    print("="*50)
    print("  台灯终端 - AI学伴 (AEC v2)")
    print("="*50)
    print(f"> xiaozhi: {CONFIG['xz_url']}")
    print(f"> 本地:    http://{CONFIG['host']}:{CONFIG['port']}")
    print()

    bridge = WebBridge(CONFIG)
    print("> 启动Web...")
    await bridge.start()

    url = f"http://{CONFIG['host']}:{CONFIG['port']}"
    webbrowser.open(url)
    print(f"> 浏览器已打开")

    print("> 连接xiaozhi...")
    ok = await bridge.connect_xz()
    print(f"> {'就绪!' if ok else '连接失败'}")

    print("> Ctrl+C 退出")
    try: await bridge.wait()
    except KeyboardInterrupt: print("\n> 关闭...")
    finally: await bridge.close()

if __name__=="__main__":
    asyncio.run(main())
