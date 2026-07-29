# Author: mjw
# Date: 2026-07-07
"""
台灯终端 - AI学伴语音对话入口脚本
一键启动: python run.py
Python端用sounddevice直接操作硬件麦克风/扬声器
浏览器只做遥控器 (发命令、看状态、看文字)
"""

import asyncio
import webbrowser
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import WebBridge

# ==================== 硬编码配置 (无需用户修改) ====================
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
    print("  台灯终端 - AI学伴语音对话")
    print("=" * 50)
    print(f"> xiaozhi服务: {CONFIG['xiaozhi_ws_url']}")
    print(f"> 本地端口:   {CONFIG['local_port']}")
    print()

    bridge = WebBridge(CONFIG)

    print("> 启动Web服务...")
    await bridge.start_server()

    print("> 初始化音频设备(麦克风+扬声器)...")
    await bridge.start_audio()

    print("> 启动唤醒词检测...")
    ww_ok = await bridge.start_wake_word()
    if ww_ok:
        print("> ✓ 唤醒词就绪, 说「小智小智」即可唤醒")
    else:
        print("> 唤醒词未启用, 需手动点击按钮")

    url = f"http://{CONFIG['local_host']}:{CONFIG['local_port']}"
    print(f"> 打开浏览器: {url}")
    webbrowser.open(url)

    print("> 连接xiaozhi服务...")
    try:
        ok = await bridge.connect_xiaozhi()
        if ok:
            print("> ✓ 就绪, 点击浏览器按钮开始对话")
        else:
            print("> ✗ xiaozhi连接失败, 请检查网络")

        print("> 按 Ctrl+C 退出...")
        try:
            await bridge.wait_closed()
        except KeyboardInterrupt:
            print("\n> 正在关闭...")
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
