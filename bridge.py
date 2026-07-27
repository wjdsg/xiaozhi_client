# Author: mjw
# Date: 2026-07-07
"""
bridge.py - AI学伴Web控制桥接
直接复用 py-xiaozhi 的 AudioCodec(sounddevice) + WebsocketProtocol
浏览器只做远程遥控 (发命令、看状态、看文字)，不传音频
"""

from collections import deque
import asyncio
import json
import os
import pickle
import random
import sys
import time
import traceback

import numpy as np
import websockets
from aiohttp import web, WSMsgType

# 导入本地精简版核心模块(已从py-xiaozhi抽取到本项目src/) — 必须先加载opus DLL再导入audio_codec
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.utils.opus_loader import setup_opus
from src.utils.config_manager import ConfigManager
from src.constants.constants import DeviceState, AudioConfig

setup_opus()  # 先加载opus.dll, 再导入依赖opuslib的模块

from src.audio_codecs.audio_codec import AudioCodec  # import opuslib, 此时DLL已就绪
from src.audio_codecs.energy_detector import EnergyDetector

# ==================== 常量 ====================
SAMPLE_RATE_IN = 16000
SAMPLE_RATE_OUT = 24000
FRAME_DURATION_MS = 20
VAD_THRESHOLD = 800  # 声音检测阈值(Int16幅值)


# ==================== Xiaozhi协议客户端 (直接连服务端) ====================
class XiaozhiClient:
    def __init__(self, url, token, device_id, client_id):
        self.url = url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Protocol-Version": "1",
            "Device-Id": device_id,
            "Client-Id": client_id,
        }
        self.ws = None
        self.connected = False
        self.on_json = None
        self.on_audio = None
        self.on_state_change = None
        self._msg_task = None

    async def connect(self):
        import ssl
        ssl_ctx = ssl._create_unverified_context() if self.url.startswith("wss://") else None
        try:
            self.ws = await websockets.connect(
                self.url, ssl=ssl_ctx, additional_headers=self.headers,
                ping_interval=20, ping_timeout=20, close_timeout=10,
                max_size=10 * 1024 * 1024,
            )
        except TypeError:
            self.ws = await websockets.connect(
                self.url, ssl=ssl_ctx, extra_headers=self.headers,
                ping_interval=20, ping_timeout=20, close_timeout=10,
                max_size=10 * 1024 * 1024,
            )

        hello = {
            "type": "hello", "version": 1, "features": {"mcp": True},
            "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": SAMPLE_RATE_IN,
                             "channels": 1, "frame_duration": FRAME_DURATION_MS},
        }
        await self.ws.send(json.dumps(hello))
        print("[Xiaozhi] 已发送hello")

        try:
            resp = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            data = json.loads(resp)
            print(f"[Xiaozhi] 收到: type={data.get('type')}, keys={list(data.keys())}")
            if data.get("type") == "hello":
                self.connected = True
                self._msg_task = asyncio.create_task(self._msg_loop())
                print("[Xiaozhi] 握手成功 ✓")
                if self.on_state_change:
                    self.on_state_change("idle")
                return True
        except asyncio.TimeoutError:
            print("[Xiaozhi] hello响应超时")
        return False

    async def _msg_loop(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, str):
                    data = json.loads(msg)
                    t = data.get("type", "?")
                    if t in ("stt", "tts", "llm"):
                        snippet = str(data.get("text", data.get("state", "")))[:40]
                        print(f"[Xiaozhi] ← {t}: {snippet}")
                    if self.on_json:
                        self.on_json(data)
                elif isinstance(msg, bytes):
                    if self.on_audio:
                        await self.on_audio(msg)  # 内联解码, 不创建任务
        except websockets.ConnectionClosed as e:
            print(f"[Xiaozhi] 连接关闭: {e}")
        except Exception as e:
            print(f"[Xiaozhi] 错误: {e}")
        finally:
            was = self.connected
            self.connected = False
            if was and self.on_state_change:
                self.on_state_change("disconnected")

    async def send_text(self, text):
        if self.ws and self.connected:
            await self.ws.send(text)

    async def send_audio(self, opus_data):
        if self.ws and self.connected:
            await self.ws.send(opus_data)

    async def close(self):
        self.connected = False
        if self._msg_task:
            self._msg_task.cancel()
        if self.ws:
            await self.ws.close()


# ==================== WebBridge: 硬件音频 + 浏览器遥控 ====================
class WebBridge:
    def __init__(self, config):
        self.config = config
        self.xiaozhi = None
        self.codec: AudioCodec | None = None
        self.browser_ws_set = set()
        self.app = web.Application()
        self._setup_routes()
        self._shutdown_event = asyncio.Event()
        self._keep_listening = False
        self._device_state = DeviceState.IDLE
        self._main_loop = asyncio.get_event_loop()  # 保存主事件循环供音频线程使用
        self._wake_word_detector = None
        self._wake_word_enabled = False
        self._energy_detector: EnergyDetector | None = None
        self._idle_timer = None
        self._tts_cache = []            # [(text, [opus_bytes, ...]), ...]
        self._current_opus_buf = []     # 当前句子的Opus帧
        self._current_tts_text = ""
        self._timer_task: asyncio.Task | None = None  # 当前闹钟任务
        self._light_level: int = 0  # 灯泡档位: 0关 1弱 2中等 3全亮
        self._companion_task = None
        self._voice_start_time = 0.0
        self._wake_detect_time = 0.0
        self._wake_response_pcm = None  # 预缓存唤醒应答音PCM
        self._last_send_time = 0.0
        self._speech_start_time = 0.0   # 本轮对话首次音频发送时间
        self._pre_listen_opus = deque(maxlen=50)  # 打断前缓存的 Opus 帧(约1秒)
        self._log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")
        self._last_xiaozhi_attempt = 0.0  # xiaozhi重连冷却时间戳

    def _log(self, msg):
        t = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{t}] {msg}\n"
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception as e:
            print(f"[Bridge] 日志写入失败: {e}")

    def _setup_routes(self):
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/ws", self._handle_browser_ws)

    # ========== 服务器启动 ==========
    async def start_server(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.config["local_host"], self.config["local_port"])
        await site.start()
        print(f"[Bridge] HTTP服务: http://{self.config['local_host']}:{self.config['local_port']}")
        self._log("服务启动")

    async def start_audio(self):
        """初始化硬件音频: 麦克风采集 + 扬声器播放"""
        self._load_tts_cache()  # 启动时加载上次对话的TTS缓存
        print("[Bridge] 初始化音频设备...")
        self.codec = AudioCodec()
        await self.codec.initialize()
        self.codec.set_encoded_callback(self._on_mic_opus)
        # VAD: 追踪首音时间用于延迟统计
        class _VAD:
            def on_audio_data(self, audio_data):
                if int(np.max(np.abs(audio_data))) > VAD_THRESHOLD:
                    self.bridge._voice_start_time = time.time()
        vad = _VAD()
        vad.bridge = self
        self.codec.add_audio_listener(vad)
        # 端侧能量检测器：用于 TTS 播放时的语音打断
        cfg = ConfigManager.get_instance()
        aec_opts = cfg.get_config("AEC_OPTIONS", {})
        self._energy_detector = EnergyDetector(
            threshold_rms=aec_opts.get("ENERGY_THRESHOLD_RMS", 0.008),
            hold_frames=aec_opts.get("ENERGY_HOLD_FRAMES", 12),
            cooldown_ms=aec_opts.get("ENERGY_COOLDOWN_MS", 800),
        )
        self._energy_detector.set_interrupt_callback(self._trigger_energy_interrupt)
        self.codec.add_audio_listener(self._energy_detector)
        print("[Bridge] 音频设备就绪 ✓")
        await self._preload_wake_response()

    async def start_wake_word(self):
        """初始化并启动唤醒词检测"""
        try:
            # 指向我们自己的models目录(含"灵犀台灯"唤醒词)
            import os as _os
            _local_models = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "models")
            config = ConfigManager.get_instance()
            config.update_config("WAKE_WORD_OPTIONS.MODEL_PATH", _local_models)
            # KWS模型很小, 单线程推理比多线程更快(实测4线程24ms→1线程14ms)且省CPU,
            # 减少与主线程Opus编解码/WebSocket的CPU争抢
            config.update_config("WAKE_WORD_OPTIONS.NUM_THREADS", 1)

            from src.audio_processing.wake_word_detect import WakeWordDetector
            self._wake_word_detector = WakeWordDetector()
            if not getattr(self._wake_word_detector, "enabled", False):
                print("[Bridge] 唤醒词功能已在配置中禁用")
                self._wake_word_detector = None
                return False

            self._wake_word_detector.on_detected(self._on_wake_word)
            ok = await self._wake_word_detector.start(self.codec)
            if ok:
                self._wake_word_enabled = True
                print("[Bridge] 唤醒词检测已启动 ✓")
                return True
            else:
                self._wake_word_detector = None
                return False
        except Exception as e:
            print(f"[Bridge] 唤醒词启动失败: {e}")
            traceback.print_exc()
            self._wake_word_detector = None
            return False

    async def toggle_wake_word(self):
        """切换唤醒词开关"""
        if self._wake_word_enabled:
            await self._stop_wake_word()
            return False
        else:
            return await self.start_wake_word()

    async def _stop_wake_word(self):
        if self._wake_word_detector:
            await self._wake_word_detector.stop()
        self._wake_word_detector = None
        self._wake_word_enabled = False
        print("[Bridge] 唤醒词检测已停止")

    async def _on_wake_word(self, wake_word, full_text):
        """唤醒词检测回调 → 自动开始对话"""
        self._wake_detect_time = time.time()
        voice_latency = (self._wake_detect_time - self._voice_start_time) if self._voice_start_time else 0
        print(f"[Bridge] 唤醒词: {wake_word} | 首音→唤醒: {voice_latency:.2f}s")
        # 断连状态 → 先重连xiaozhi
        if not self.xiaozhi or not self.xiaozhi.connected:
            self._stop_companion()
            if self.xiaozhi:
                await self.xiaozhi.close()
            ok = await self.connect_xiaozhi()
            if not ok:
                await self._broadcast_json({"type": "state", "state": "disconnected"})
                return
        # 如果正在说话, 先打断
        if self._device_state == DeviceState.SPEAKING:
            await self.xiaozhi.send_text(json.dumps({"type": "abort"}))
            if self.codec:
                await self.codec.clear_audio_queue()
            await asyncio.sleep(0.05)

        self._keep_listening = True
        await self._play_wake_response()
        await self.xiaozhi.send_text(json.dumps(
            {"type": "listen", "state": "start", "mode": "auto"}))
        await self._set_state(DeviceState.LISTENING)
        await self._broadcast_json({"type": "state", "state": "listening"})

    def _trigger_energy_interrupt(self):
        """音频线程回调 -> 调度到主事件循环执行打断逻辑"""
        self._main_loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._on_energy_interrupt())
        )

    async def _on_energy_interrupt(self):
        if self._device_state != DeviceState.SPEAKING:
            return
        print("[Bridge] 能量打断: 检测到用户语音, 停止TTS播报")
        if self.xiaozhi and self.xiaozhi.connected:
            await self.xiaozhi.send_text(json.dumps({"type": "abort"}))
        if self.codec:
            await self.codec.clear_audio_queue()
        self._keep_listening = True
        await self.xiaozhi.send_text(json.dumps(
            {"type": "listen", "state": "start", "mode": "auto"}))
        # flush 打断前缓存的 Opus 帧, 确保开头语音不丢失
        buf = list(self._pre_listen_opus)
        self._pre_listen_opus.clear()
        for opus_data in buf:
            await self.xiaozhi.send_audio(opus_data)
        await self._set_state(DeviceState.LISTENING)
        await self._broadcast_json({"type": "state", "state": "listening"})

    async def connect_xiaozhi(self):
        print(f"[Bridge] 连接xiaozhi: {self.config['xiaozhi_ws_url']}")
        self.xiaozhi = XiaozhiClient(
            self.config["xiaozhi_ws_url"], self.config["xiaozhi_token"],
            self.config["device_id"], self.config["client_id"],
        )
        self.xiaozhi.on_json = self._on_xiaozhi_json
        self.xiaozhi.on_audio = self._on_xiaozhi_audio
        self.xiaozhi.on_state_change = self._on_xiaozhi_state
        ok = await self.xiaozhi.connect()
        if ok:
            self._stop_companion()  # 重连后停掉本地复播
            await self._broadcast_json({"type": "state", "state": "idle"})
        else:
            await self._broadcast_json({"type": "state", "state": "disconnected"})
            await self._broadcast_json({"type": "error", "message": "无法连接xiaozhi服务"})
        return ok

    async def wait_closed(self):
        await self._shutdown_event.wait()

    async def close(self):
        self._shutdown_event.set()
        self._cancel_idle_timer()
        self._save_tts_cache()  # 关闭时落盘
        if self._wake_word_detector:
            await self._wake_word_detector.stop()
        if self.xiaozhi:
            await self.xiaozhi.close()
        if self.codec:
            await self.codec.close()
        for ws in list(self.browser_ws_set):
            try: await ws.close()
            except Exception: pass
        print("[Bridge] 已关闭")

    # ========== HTTP ==========
    async def _handle_index(self, request):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
        return web.FileResponse(path)

    # ========== 浏览器 WebSocket ==========
    async def _handle_browser_ws(self, request):
        ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024, heartbeat=15.0)
        await ws.prepare(request)
        self.browser_ws_set.add(ws)
        print(f"[Bridge] 浏览器连接 (共{len(self.browser_ws_set)})")

        # 发送当前状态, 如果xiaozhi断连则自动重连
        if self.xiaozhi and self.xiaozhi.connected:
            await ws.send_json({"type": "state", "state": "idle"})
        elif self.xiaozhi and not self.xiaozhi.connected:
            now = time.time()
            if now - self._last_xiaozhi_attempt < 6:
                await ws.send_json({"type": "state", "state": "connecting"})
            else:
                self._last_xiaozhi_attempt = now
                print("[Bridge] xiaozhi已断连, 自动重连...")
                self._stop_companion()
                ok = await self.connect_xiaozhi()
                if ok:
                    await ws.send_json({"type": "state", "state": "idle"})
                else:
                    await ws.send_json({"type": "state", "state": "disconnected"})
        else:
            await ws.send_json({"type": "state", "state": "connecting"})
        await ws.send_json({"type": "wake_word", "enabled": self._wake_word_enabled})
        await ws.send_json({"type": "light_state", "level": self._light_level})

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._on_browser_cmd(data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                    break
        except ConnectionResetError:
            pass
        finally:
            self.browser_ws_set.discard(ws)
            print(f"[Bridge] 浏览器断开 (共{len(self.browser_ws_set)})")
        return ws

    async def _on_browser_cmd(self, data):
        t = data.get("type", "")
        print(f"[Bridge] 浏览器命令: {t}")
        if t == "start_listening":
            if not self.xiaozhi or not self.xiaozhi.connected:
                await self._broadcast_json({"type": "error", "message": "xiaozhi未连接"})
                return
            self._keep_listening = True
            self._speech_start_time = 0.0  # 重置, 等首帧音频触发
            await self.xiaozhi.send_text(json.dumps(
                {"type": "listen", "state": "start", "mode": "auto"}))
            await self._set_state(DeviceState.LISTENING)
            await self._broadcast_json({"type": "state", "state": "listening"})
            print("[Bridge] 已发送listen start + 状态→listening")
        elif t == "stop_listening":
            self._keep_listening = False
            await self.xiaozhi.send_text(json.dumps({"type": "listen", "state": "stop"}))
            await self._set_state(DeviceState.IDLE)
            await self._broadcast_json({"type": "state", "state": "idle"})
        elif t == "abort":
            self._keep_listening = False
            await self.xiaozhi.send_text(json.dumps({"type": "abort"}))
            if self.codec:
                await self.codec.clear_audio_queue()
            await self._set_state(DeviceState.IDLE)
            await self._broadcast_json({"type": "state", "state": "idle"})
        elif t == "toggle_wake_word":
            enabled = await self.toggle_wake_word()
            await self._broadcast_json({"type": "wake_word", "enabled": enabled})
        elif t == "set_timer":
            mins = float(data.get("minutes", 5))
            label = data.get("label", "")
            asyncio.create_task(self.start_timer(mins, label))
        elif t == "reconnect":
            self._keep_listening = False
            if self.xiaozhi:
                await self.xiaozhi.close()
            await self.connect_xiaozhi()

    # ========== 麦克风 → xiaozhi ==========
    def _on_mic_opus(self, opus_data):
        """AudioCodec回调(音频线程): 麦克风采集→Opus编码→发送到xiaozhi
        对齐 py-xiaozhi AudioPlugin._on_encoded_audio (audio.py:193)
        """
        if self._device_state == DeviceState.SPEAKING:
            self._pre_listen_opus.append(opus_data)
            return
        if self._device_state != DeviceState.LISTENING:
            return
        if not self.xiaozhi or not self.xiaozhi.connected:
            return
        # 直接在事件循环线程发送，不创建额外 task
        # 对齐 py-xiaozhi 的 _schedule_send_audio → protocol.send_audio
        if not self._speech_start_time:
            self._speech_start_time = time.time()
        self._main_loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.xiaozhi.send_audio(opus_data))
        )

    # ========== Xiaozhi → 扬声器 + 浏览器 ==========
    def _on_xiaozhi_json(self, data):
        t = data.get("type", "")
        if t == "stt":
            if self._speech_start_time and data.get("text"):
                asr_latency = time.time() - self._speech_start_time
                msg = f"首音→STT返回: {asr_latency:.2f}s text={data.get('text','')[:30]}"
                print(f"[Log] {msg}")
                self._log(msg)
            asyncio.create_task(self._broadcast_json({
                "type": "stt", "text": data.get("text", ""),
            }))
        elif t == "llm":
            asyncio.create_task(self._broadcast_json({
                "type": "llm",
                "emotion": data.get("emotion", "neutral"),
            }))
        elif t == "tts":
            state = data.get("state", "")
            text = data.get("text", "")
            if state == "start" and self._speech_start_time:
                tts_latency = time.time() - self._speech_start_time
                msg = f"首音→首句播报: {tts_latency:.2f}s text={text[:30]}"
                print(f"[Log] {msg}")
                self._log(msg)
            # 处理 sentence_start 里的文字 (字幕)
            if state == "sentence_start" and data.get("text"):
                asyncio.create_task(self._broadcast_json(
                    {"type": "tts", "state": state, "text": data.get("text")}))
            print(f"[Bridge] TTS: {state}" + (f" text={text[:40]}..." if text else ""))
            if state == "start":
                self._current_opus_buf = []
                self._current_tts_text = text or ""
                # 兜底：LLM 没走 function_call 时, 从 TTS 文本解析闹钟命令
                if text:
                    self._try_parse_timer_from_text(text)
                    self._try_parse_cancel_from_text(text)
                    self._try_parse_light_from_text(text)
                asyncio.create_task(self._set_state(DeviceState.SPEAKING))  # 切到SPEAKING, 唤醒词仍活跃可打断
                asyncio.create_task(self._broadcast_json(
                    {"type": "state", "state": "speaking"}))
            if text:
                asyncio.create_task(self._broadcast_json(
                    {"type": "tts", "state": state, "text": text}))
            elif state != "start":
                asyncio.create_task(self._broadcast_json(
                    {"type": "tts", "state": state}))
            if state == "stop":
                if self._current_opus_buf and self._current_tts_text:
                    self._tts_cache.append((self._current_tts_text, list(self._current_opus_buf)))
                    # 只保留最近10条, 避免缓存膨胀
                    if len(self._tts_cache) > 10:
                        self._tts_cache = self._tts_cache[-10:]
                    print(f"[Bridge] TTS缓存已保存: {self._current_tts_text[:30]}... (共{len(self._tts_cache)}条)")
                    self._current_opus_buf = []
                if self._keep_listening:
                    asyncio.create_task(self._auto_restart())
        elif t == "mcp":
            self._handle_mcp(data)
        elif t == "function_call":
            self._handle_function_call(data)
        else:
            print(f"[Bridge] 未知消息类型: {t}, keys={list(data.keys())}")

    def _handle_mcp(self, data):
        """处理服务端 MCP JSON-RPC 2.0 消息: initialize / tools/list / tools/call"""
        payload = data.get("payload", {})
        method = payload.get("method", "")
        msg_id = payload.get("id")
        params = payload.get("params", {})
        print(f"[Bridge] MCP: method={method}, id={msg_id}")

        if method == "initialize":
            asyncio.create_task(self._mcp_initialize(msg_id))
        elif method == "tools/list":
            asyncio.create_task(self._mcp_tools_list(msg_id))
        elif method == "tools/call":
            asyncio.create_task(self._mcp_tools_call(msg_id, params))
        else:
            print(f"[Bridge] MCP: 未实现的方法 {method}")

    # ========== MCP 协议处理器 ==========
    async def _mcp_reply(self, msg_id, result=None, error_msg=None):
        """发送 JSON-RPC 2.0 响应回 xiaozhi 服务端"""
        payload = {"jsonrpc": "2.0", "id": msg_id}
        if error_msg:
            payload["error"] = {"message": error_msg}
        else:
            payload["result"] = result if result is not None else {}
        if self.xiaozhi and self.xiaozhi.connected:
            await self.xiaozhi.send_text(json.dumps({"type": "mcp", "payload": payload}))

    async def _mcp_initialize(self, msg_id):
        await self._mcp_reply(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "台灯终端", "version": "1.0.0"},
        })

    async def _mcp_tools_list(self, msg_id):
        tools = [{
            "name": "set_timer",
            "description": (
                "设置一个倒计时闹钟,到时间后台灯扬声器会播放闹铃声提醒用户。"
                "当用户想让台灯帮ta定时提醒时使用,例如'帮我设5分钟闹钟'、'倒计时10分钟'、"
                "'提醒我15分钟后休息'、'定一个25分钟的番茄钟'等。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "number",
                        "description": "倒计时的分钟数,必须是正整数,如5表示5分钟",
                    },
                    "label": {
                        "type": "string",
                        "description": "闹钟标签,可选,如'休息时间到'",
                    },
                },
                "required": ["minutes"],
            },
        },
        {
            "name": "cancel_timer",
            "description": (
                "取消当前正在运行的倒计时闹钟。"
                "当用户说'关闭闹钟'、'取消闹钟'、'不用提醒了'、'把闹钟关掉'时使用。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "light_toggle",
            "description": (
                "切换台灯灯泡的开关状态。关灯时打开(全亮),开灯时关闭。"
                "当用户说'开灯'、'关灯'、'把灯打开'、'把灯关掉'、"
                "'灯亮一点'、'灯暗一点'时使用。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "set_brightness",
            "description": (
                "设置台灯灯泡的亮度档位。0=关, 1=弱光, 2=中等, 3=全亮。"
                "当用户说'灯光调到中等'、'灯泡暗一点'、'调到最亮'、"
                "'亮度设为弱'、'把灯调到最大'时使用。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "integer",
                        "description": "亮度档位: 0关/1弱/2中等/3全亮",
                        "minimum": 0,
                        "maximum": 3,
                    },
                },
                "required": ["level"],
            },
        }]
        await self._mcp_reply(msg_id, {"tools": tools})

    async def _mcp_tools_call(self, msg_id, params):
        name = params.get("name", "")
        args = params.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        print(f"[Bridge] MCP tools/call: {name}, args={json.dumps(args, ensure_ascii=False)}")
        if name == "set_timer":
            minutes = float(args.get("minutes", 0))
            label = args.get("label", "") or f"{int(minutes)}分钟"
            if minutes <= 0:
                await self._mcp_reply(msg_id, error_msg="分钟数必须大于0")
                return
            asyncio.create_task(self.start_timer(minutes, label))
            result_text = f"已设置{label}闹钟,{int(minutes)}分钟后提醒"
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
        elif name == "cancel_timer":
            if self._timer_task and not self._timer_task.done():
                self._timer_task.cancel()
                self._timer_task = None
                result_text = "闹钟已取消"
            else:
                result_text = "当前没有正在运行的闹钟"
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
            await self._broadcast_json({"type": "timer_cancelled"})
        elif name == "light_toggle":
            if self._light_level == 0:
                self._light_level = 3
                result_text = "灯已打开，全亮"
            else:
                self._light_level = 0
                result_text = "灯已关闭"
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
            await self._broadcast_json({"type": "light_state", "level": self._light_level})
        elif name == "set_brightness":
            level = int(args.get("level", 3))
            level = max(0, min(3, level))
            self._light_level = level
            labels = {0: "灯已关闭", 1: "灯光已调到弱光", 2: "灯光已调到中等", 3: "灯光已调到全亮"}
            result_text = labels.get(level, f"亮度已设为{level}")
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
            await self._broadcast_json({"type": "light_state", "level": self._light_level})
        else:
            await self._mcp_reply(msg_id, error_msg=f"未知工具: {name}")

    def _handle_function_call(self, data):
        """处理OpenAI风格的function_call消息"""
        name = data.get("name", "") or data.get("function_name", "")
        args_str = data.get("arguments", "{}")
        if isinstance(args_str, str):
            try: args = json.loads(args_str)
            except: args = {}
        else:
            args = args_str
        print(f"[Bridge] FunctionCall: name={name}, args={json.dumps(args, ensure_ascii=False)}")
        self._try_start_timer(name, args)
        self._try_cancel_timer(name, args)
        self._try_light_command(name, args)

    def _try_light_command(self, name, args):
        """识别灯泡控制意图"""
        light_keywords = ("light_toggle", "light_control", "set_brightness",
                          "light", "灯光", "灯泡", "开灯", "关灯", "亮度")
        cn_low = ("弱", "暗", "低", "小", "暗一点", "小一点")
        cn_mid = ("中等", "中", "适中", "中档", "中等亮度")
        cn_high = ("全亮", "强", "高", "亮", "大", "最亮", "最高", "最大", "开", "打开", "亮一点", "亮一些")
        cn_off = ("关", "关闭", "关掉")

        if not name or not any(kw in str(name).lower() for kw in light_keywords):
            return

        # 尝试从 args 获取 level
        level = args.get("level")
        if level is not None:
            self._light_level = max(0, min(3, int(level)))
            print(f"[Bridge] 意图识别灯光: level={self._light_level}")
            asyncio.create_task(self._broadcast_json({"type": "light_state", "level": self._light_level}))
            return

        # 从 name 或 label/action 等字段解析
        text = str(name).lower()
        extra = str(args.get("label", "") or args.get("action", "") or args.get("state", ""))
        combined = text + extra

        if any(kw in combined for kw in cn_off):
            self._light_level = 0
        elif any(kw in combined for kw in cn_low):
            self._light_level = 1
        elif any(kw in combined for kw in cn_mid):
            self._light_level = 2
        elif any(kw in combined for kw in cn_high):
            self._light_level = 3
        else:
            # 无法判断: toggle
            self._light_level = 0 if self._light_level > 0 else 3

        print(f"[Bridge] 意图识别灯光: level={self._light_level}")
        asyncio.create_task(self._broadcast_json({"type": "light_state", "level": self._light_level}))

    def _try_parse_light_from_text(self, text):
        """从 LLM 文本回复中解析灯光命令"""
        import re
        cn_low = ("弱", "暗", "低", "小")
        cn_mid = ("中等", "中", "适中")
        cn_high = ("全亮", "最亮", "最高", "最大")
        cn_off = ("关", "关闭", "关掉")
        cn_on = ("开", "打开", "打开灯")

        original_level = self._light_level

        if any(phrase in text for phrase in cn_off):
            self._light_level = 0
        elif any(phrase in text for phrase in cn_low):
            self._light_level = 1
        elif any(phrase in text for phrase in cn_mid):
            self._light_level = 2
        elif any(phrase in text for phrase in cn_high):
            self._light_level = 3
        elif any(phrase in text for phrase in cn_on):
            self._light_level = 3
        else:
            return

        if self._light_level != original_level:
            print(f"[Bridge] 文本识别灯光: level={self._light_level} (原文: {text[:50]})")
            asyncio.create_task(self._broadcast_json({"type": "light_state", "level": self._light_level}))

    def _try_start_timer(self, name, args):
        """识别闹钟相关意图并启动计时"""
        timer_keywords = ("set_timer", "start_timer", "create_timer", "set_alarm",
                          "timer", "alarm", "闹钟", "计时", "倒计时", "定时")
        if not name or not any(kw in str(name).lower() for kw in timer_keywords):
            return
        minutes = args.get("minutes", 0) or args.get("duration", 0) or args.get("seconds", 0) / 60
        if isinstance(minutes, str):
            try: minutes = float(minutes)
            except: minutes = 0
        if minutes <= 0:
            return
        label = args.get("label", "") or args.get("name", "") or f"{int(minutes)}分钟"
        print(f"[Bridge] 意图识别闹钟: {label}")
        asyncio.create_task(self.start_timer(minutes, label))

    def _try_cancel_timer(self, name, args):
        """识别取消闹钟意图 (function_call 兜底)"""
        cancel_keywords = ("cancel_timer", "stop_timer", "cancel",
                           "取消", "关闭", "关掉", "停", "不用")
        if not name or not any(kw in str(name).lower() for kw in cancel_keywords):
            return
        cn_timer = ("闹钟", "计时", "定时", "提醒", "alarm", "timer")
        combined = str(name).lower() + str(args.get("label", "") or args.get("name", ""))
        if any(kw in combined for kw in cn_timer) or "cancel_timer" in str(name).lower():
            self._do_cancel_timer()
        elif any(kw in combined for kw in ("取消", "关闭", "关掉", "停", "不用")) and any(kw in combined for kw in ("闹钟", "计时", "提醒", "alarm", "timer")):
            self._do_cancel_timer()

    def _do_cancel_timer(self):
        """实际执行取消闹钟"""
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None
            print("[Bridge] 意图识别取消闹钟 ✓")
            asyncio.create_task(self._broadcast_json({"type": "timer_cancelled"}))
        else:
            print("[Bridge] 意图识别取消闹钟: 当前无运行中的闹钟")

    def _parse_cn_number(self, s):
        cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if not s:
            return None
        if "十" in s:
            parts = s.split("十", 1)
            left = cn.get(parts[0], 1) if parts[0] else 1
            right = cn.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
            return left * 10 + right
        val = cn.get(s)
        return val if val is not None else (0.5 if s == "半" else None)

    def _try_parse_timer_from_text(self, text):
        """从 LLM 文本回复中解析闹钟命令, 兜底 function_call 失败的情况"""
        import re
        patterns = [
            (r"(\d+)\s*分钟", lambda m: int(m.group(1))),
            (r"(\d+)\s*分\s*钟", lambda m: int(m.group(1))),
            (r"([一二三四五六七八九十半]+)\s*分钟", lambda m: self._parse_cn_number(m.group(1))),
            (r"(\d+)\s*分\b", lambda m: int(m.group(1))),
        ]
        for pattern, extract in patterns:
            match = re.search(pattern, text)
            if match:
                minutes = extract(match)
                if minutes is not None and minutes > 0 and minutes <= 120:
                    print(f"[Bridge] 文本识别闹钟: {minutes}分钟 (原文: {text[:50]})")
                    asyncio.create_task(self.start_timer(minutes))
                    return

    def _try_parse_cancel_from_text(self, text):
        """从 LLM 文本回复中解析取消闹钟命令"""
        cn_cancel = ("取消闹钟", "关闭闹钟", "关掉闹钟", "把闹钟关", "不用提醒", "不用闹钟",
                     "停掉闹钟", "停止闹钟", "不要闹钟", "取消计时", "关闭计时")
        if any(phrase in text for phrase in cn_cancel):
            self._do_cancel_timer()

    async def _on_xiaozhi_audio(self, opus_data):
        """xiaozhi返回TTS音频 → 扬声器播放"""
        if not self.codec:
            return
        try:
            # 缓存原始Opus数据(供本地复播用)
            self._current_opus_buf.append(opus_data)
            # py-xiaozhi标准播放路径
            await self.codec.write_audio(opus_data)
        except Exception as e:
            print(f"[Bridge] TTS播放异常: {e}")

    def _on_xiaozhi_state(self, state):
        asyncio.create_task(self._broadcast_json({"type": "state", "state": state}))
        if state == "disconnected":
            self._cancel_idle_timer()
            asyncio.create_task(self._try_reconnect())

    async def _auto_restart(self):
        await asyncio.sleep(0.3)
        if self._keep_listening and self.xiaozhi and self.xiaozhi.connected:
            await self.codec.clear_audio_queue()
            await self.xiaozhi.send_text(json.dumps(
                {"type": "listen", "state": "start", "mode": "auto"}))
            await self._set_state(DeviceState.LISTENING)
            await self._broadcast_json({"type": "state", "state": "listening"})

    async def _try_reconnect(self):
        for i in range(5):
            await asyncio.sleep(3)
            if self.xiaozhi and await self.xiaozhi.connect():
                await self._broadcast_json({"type": "state", "state": "idle"})
                return
        await self._broadcast_json({"type": "error", "message": "服务连接失败, 请刷新"})

    async def _set_state(self, state):
        self._device_state = state
        if state == DeviceState.IDLE:
            self._start_idle_timer()
            self._pre_listen_opus.clear()
            if self._wake_word_detector:
                self._wake_word_detector.paused = False
            if self._energy_detector:
                self._energy_detector.disable()
        elif state == DeviceState.LISTENING:
            self._cancel_idle_timer()
            self._pre_listen_opus.clear()
            if self._wake_word_detector:
                self._wake_word_detector.paused = True
            if self._energy_detector:
                self._energy_detector.disable()
        elif state == DeviceState.SPEAKING:
            if self._wake_word_detector:
                self._wake_word_detector.paused = False
            if self._energy_detector:
                self._energy_detector.enable()  # 允许语音打断

    def _start_idle_timer(self):
        self._cancel_idle_timer()
        async def _timeout():
            await asyncio.sleep(30)
            if self._device_state == DeviceState.IDLE:
                print("[Bridge] 30秒无对话, 切换本地陪伴")
                self._keep_listening = False
                if self.xiaozhi:
                    await self.xiaozhi.close()
                self._save_tts_cache()  # 只在断连时落盘
                await self._broadcast_json({"type": "state", "state": "disconnected"})
                self._start_companion()
        self._idle_timer = asyncio.create_task(_timeout())

    def _cancel_idle_timer(self):
        if self._idle_timer and not self._idle_timer.done():
            self._idle_timer.cancel()
        self._idle_timer = None

    # ========== TTS磁盘缓存 ==========
    @property
    def _cache_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_cache.pkl")

    def _save_tts_cache(self):
        """后台线程写磁盘, 不阻塞事件循环"""
        if not self._tts_cache:
            return
        import threading
        cache_copy = list(self._tts_cache)  # 避免并发修改
        cache_path = self._cache_path
        def _write():
            try:
                with open(cache_path, "wb") as f:
                    pickle.dump(cache_copy, f)
            except Exception as e:
                print(f"[Bridge] 缓存保存失败: {e}")
        threading.Thread(target=_write, daemon=True).start()

    def _load_tts_cache(self):
        if not os.path.exists(self._cache_path):
            return
        try:
            with open(self._cache_path, "rb") as f:
                self._tts_cache = pickle.load(f)
            print(f"[Bridge] 从磁盘加载TTS缓存 ({len(self._tts_cache)}条)")
        except Exception as e:
            print(f"[Bridge] 缓存加载失败: {e}")

    # ========== 本地陪伴模式: 断联后复播缓存 ==========
    def _start_companion(self):
        if self._companion_task and not self._companion_task.done():
            return
        if not self._tts_cache:
            print("[Bridge] 无缓存语句, 跳过陪伴模式")
            return
        self._companion_task = asyncio.create_task(self._companion_loop())
        print(f"[Bridge] 本地陪伴模式启动 (缓存{len(self._tts_cache)}条)")

    def _stop_companion(self):
        if self._companion_task and not self._companion_task.done():
            self._companion_task.cancel()
        self._companion_task = None

    async def _companion_loop(self):
        """断联后播放缓存的最后一句道别语"""
        if not self._tts_cache:
            return
        text, opus_frames = self._tts_cache[-1]
        print(f"[陪伴] 播放缓存道别: {text[:30]}...")
        await self._broadcast_json({"type": "state", "state": "speaking"})
        await self._broadcast_json({"type": "tts", "state": "start", "text": text})
        for opus in opus_frames:
            if self.xiaozhi and self.xiaozhi.connected:
                return
            await self.codec.write_audio(opus)
        await self._broadcast_json({"type": "tts", "state": "stop"})
        await self._broadcast_json({"type": "state", "state": "idle"})

    # ========== 闹钟记时 ==========
    async def _preload_wake_response(self):
        """启动阶段预解码唤醒应答音, 后续唤醒直接使用缓存避免FFmpeg进程开销"""
        mp3_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nihaowozai.mp3")
        if not os.path.exists(mp3_path):
            print("[Wake] 应答音文件缺失, 跳过预加载")
            return
        sample_rate = AudioConfig.OUTPUT_SAMPLE_RATE
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "quiet",
            "-i", mp3_path,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(sample_rate), "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            pcm_bytes, _ = await proc.communicate()
            if pcm_bytes:
                self._wake_response_pcm = np.frombuffer(pcm_bytes, dtype=np.int16)
                print(f"[Wake] 应答音已预缓存: {len(self._wake_response_pcm)}样本")
        except FileNotFoundError:
            print("[Wake] 未找到ffmpeg, 跳过应答音预加载")
        except Exception as e:
            print(f"[Wake] 预加载失败: {e}")

    async def _play_wake_response(self):
        """播放唤醒应答音, 优先使用预缓存PCM"""
        frame_size = AudioConfig.OUTPUT_FRAME_SIZE
        if self._wake_response_pcm is not None:
            samples = self._wake_response_pcm
            for i in range(0, len(samples), frame_size):
                frame = samples[i:i + frame_size]
                await self.codec.write_pcm_direct(frame.copy())
            return

        # 回退: FFmpeg实时解码 (冷启动/缓存失效时)
        mp3_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nihaowozai.mp3")
        if not self.codec or not os.path.exists(mp3_path):
            print("[Wake] 应答音文件缺失或音频未就绪")
            return
        sample_rate = AudioConfig.OUTPUT_SAMPLE_RATE
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "quiet",
            "-i", mp3_path,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(sample_rate), "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            pcm_bytes, _ = await proc.communicate()
        except FileNotFoundError:
            print("[Wake] 未找到ffmpeg, 跳过应答音")
            return
        except Exception as e:
            print(f"[Wake] 解码失败: {e}")
            return
        if not pcm_bytes:
            print("[Wake] 解码结果为空")
            return
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        self._wake_response_pcm = samples
        for i in range(0, len(samples), frame_size):
            frame = samples[i:i + frame_size]
            await self.codec.write_pcm_direct(frame.copy())

    async def _play_alarm_sound(self):
        """用台灯扬声器播放闹铃mp3 (FFmpeg解码为目标采样率PCM → codec直推)"""
        mp3_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clock-alarm-1.mp3")
        if not self.codec or not os.path.exists(mp3_path):
            print(f"[Alarm] 闹铃文件缺失或音频未就绪: {mp3_path}")
            return
        sample_rate = AudioConfig.OUTPUT_SAMPLE_RATE
        frame_size = AudioConfig.OUTPUT_FRAME_SIZE  # 单帧样本数(单声道)
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "quiet",
            "-i", mp3_path,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(sample_rate), "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            pcm_bytes, _ = await proc.communicate()
        except FileNotFoundError:
            print("[Alarm] 未找到ffmpeg, 无法播放闹铃")
            return
        except Exception as e:
            print(f"[Alarm] 解码失败: {e}")
            return
        if not pcm_bytes:
            print("[Alarm] 解码结果为空")
            return
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        print(f"[Alarm] 播放闹铃: {len(samples)}样本 @ {sample_rate}Hz")
        for i in range(0, len(samples), frame_size):
            frame = samples[i:i + frame_size]
            await self.codec.write_pcm_direct(frame.copy())

    async def start_timer(self, minutes, label=""):
        self._timer_task = asyncio.current_task()
        total_seconds = int(minutes * 60)
        label = label or f"{minutes}分钟"
        print(f"[Timer] 启动: {label} ({total_seconds}s)")
        await self._broadcast_json({"type": "timer_start", "seconds": total_seconds, "label": label})
        try:
            deadline = time.monotonic() + total_seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
            print(f"[Timer] 时间到: {label}")
            await self._broadcast_json({"type": "timer_done", "label": label})
            await self._play_alarm_sound()
        except asyncio.CancelledError:
            print(f"[Timer] 已取消: {label}")
            await self._broadcast_json({"type": "timer_cancelled", "label": label})
        finally:
            self._timer_task = None

    # ========== 广播 ==========
    async def _broadcast_json(self, data):
        for ws in list(self.browser_ws_set):
            try:
                await ws.send_json(data)
            except Exception:
                self.browser_ws_set.discard(ws)
