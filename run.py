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

# 仅供部署诊断/自动化验证使用；普通执行 python run.py 不需要设置。
if os.environ.get("LAMP_LOCAL_PORT"):
    CONFIG["local_port"] = int(os.environ["LAMP_LOCAL_PORT"])
ENABLE_AUDIO = os.environ.get("LAMP_DISABLE_AUDIO", "0").strip().lower() not in {"1", "true", "yes"}
OPEN_BROWSER = os.environ.get("LAMP_OPEN_BROWSER", "1").strip().lower() not in {"0", "false", "no"}

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

    try:
        print("> 启动统一Web服务...")
        await bridge.start_server()
        xiaozhi_task = asyncio.create_task(bridge.connect_xiaozhi())

        if ENABLE_AUDIO:
            print("> 启动音频设备...")
            try:
                await bridge.start_audio()
                print("> 启动唤醒词检测...")
                ww_ok = await bridge.start_wake_word()
                if ww_ok:
                    print("> [OK] 唤醒词就绪, 说「小智小智」即可唤醒")
                else:
                    print("> 唤醒词未启用, 需手动点击按钮")
            except Exception as exc:
                print(f"> [WARN] 音频设备启动失败：{exc}")
                print("> AI 听写和教材功能仍可使用；请检查设备编号和麦克风权限。")
                if bridge.codec:
                    await bridge.codec.close()
                    bridge.codec = None
        else:
            print("> [诊断模式] 已跳过音频设备和唤醒词")

        url = f"http://{CONFIG['local_host']}:{CONFIG['local_port']}"
        ok = await xiaozhi_task
        if ok:
            print("> [OK] 小智服务已连接")
        else:
            print("> [WARN] 小智连接失败，教材选词和拍照听写仍可使用")

        if OPEN_BROWSER:
            print(f"> 打开浏览器: {url}")
            webbrowser.open(url)
        else:
            print(f"> 诊断地址: {url}")

        print("> 按 Ctrl+C 退出...")
        await bridge.wait_closed()
    except KeyboardInterrupt:
        print("\n> 正在关闭...")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) == 10048:
            print(f"> [ERROR] 本地端口 {CONFIG['local_port']} 已被占用，请先关闭旧的台灯进程再重试。")
        else:
            raise
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
