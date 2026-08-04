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
from src.utils.config_manager import ConfigManager

# ==================== 可移植运行配置 ====================
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = {"local_host": "127.0.0.1", "local_port": 8765}
RUNTIME_CONFIG_PATH = os.path.join(PROJECT_DIR, "config", "runtime.json")
if os.path.isfile(RUNTIME_CONFIG_PATH):
    with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as config_file:
        import json
        CONFIG.update(json.load(config_file))

# 业务服务配置统一从 config/config.json 读取；runtime.json 只负责本地网页服务。
app_config = ConfigManager.get_instance()
CONFIG.update({
    "xiaozhi_ws_url": app_config.get_config(
        "SYSTEM_OPTIONS.NETWORK.WEBSOCKET_URL",
        "ws://10.20.149.33:5000/xiaozhi/v1/",
    ),
    "xiaozhi_token": app_config.get_config(
        "SYSTEM_OPTIONS.NETWORK.WEBSOCKET_ACCESS_TOKEN", ""
    ) or "",
    "device_id": app_config.get_config("SYSTEM_OPTIONS.DEVICE_ID", "") or "",
    "client_id": app_config.get_config("SYSTEM_OPTIONS.CLIENT_ID", "") or "",
})


async def main():
    print("=" * 50)
    print("  台灯终端 - AI学伴语音对话")
    print("=" * 50)
    print(f"> xiaozhi服务: {CONFIG['xiaozhi_ws_url']}")
    print(f"> 本地端口:   {CONFIG['local_port']}")
    print()

    bridge = WebBridge(CONFIG)

    print("> 启动Web服务 + 音频设备 + xiaozhi连接...")
    xiaozhi_task = asyncio.create_task(bridge.connect_xiaozhi())
    await asyncio.gather(bridge.start_server(), bridge.start_audio())

    print("> 启动唤醒词检测...")
    ww_ok = await bridge.start_wake_word()
    if ww_ok:
        print("> [OK] 唤醒词就绪, 说「小智小智」即可唤醒")
    else:
        print("> 唤醒词未启用, 需手动点击按钮")

    url = f"http://{CONFIG['local_host']}:{CONFIG['local_port']}"

    ok = await xiaozhi_task
    if ok:
        print("> [OK] 就绪, 点击浏览器按钮开始对话")
    else:
        print("> [ERROR] xiaozhi连接失败, 请检查网络")

    print(f"> 打开浏览器: {url}")
    webbrowser.open(url)

    print("> 按 Ctrl+C 退出...")
    try:
        await bridge.wait_closed()
    except KeyboardInterrupt:
        print("\n> 正在关闭...")
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
