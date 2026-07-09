# Author: mjw
# Date: 2026-07-09
"""
台灯终端 - AI学伴语音对话 (AEC版)
一键启动: python run.py
浏览器做AEC音频采集+播放, Python只做Opus编解码+协议转发
"""

import asyncio
import webbrowser
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import WebBridge

# ==================== 硬编码配置 ====================
CONFIG = {
    "xiaozhi_ws_url": "ws://10.150.101.33:5000/xiaozhi/v1/",
    "xiaozhi_token": "test-token",
    "device_id": "2c:db:07:09:da:31",
    "client_id": "cff1e306-cce9-4ff6-b33e-60fefabf32c4",
    "local_host": "127.0.0.1",
    "local_port": 8765,
}


async def main():
    print("=" * 50)
    print("  台灯终端 - AI学伴 (AEC版)")
    print("=" * 50)
    print(f"> xiaozhi: {CONFIG['xiaozhi_ws_url']}")
    print(f"> 本地:   http://{CONFIG['local_host']}:{CONFIG['local_port']}")
    print()

    bridge = WebBridge(CONFIG)

    print("> 启动Web服务...")
    await bridge.start_server()

    url = f"http://{CONFIG['local_host']}:{CONFIG['local_port']}"
    webbrowser.open(url)
    print(f"> 浏览器已打开: {url}")

    print("> 连接xiaozhi...")
    ok = await bridge.connect_xiaozhi()
    if ok:
        print("> 就绪! 点击[开始对话]按钮, 浏览器会请求麦克风权限")
    else:
        print("> xiaozhi连接失败, 检查网络")

    print("> Ctrl+C 退出")
    try:
        await bridge.wait_closed()
    except KeyboardInterrupt:
        print("\n> 关闭中...")
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
