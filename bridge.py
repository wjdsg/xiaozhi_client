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
from src.utils.page_inject import render_index_html
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
            try:
                self.ws = await websockets.connect(
                    self.url, ssl=ssl_ctx, additional_headers=self.headers,
                    ping_interval=20, ping_timeout=20, close_timeout=10,
                    open_timeout=5, max_size=10 * 1024 * 1024,
                )
            except TypeError:
                self.ws = await websockets.connect(
                    self.url, ssl=ssl_ctx, extra_headers=self.headers,
                    ping_interval=20, ping_timeout=20, close_timeout=10,
                    open_timeout=5, max_size=10 * 1024 * 1024,
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
                    print("[Xiaozhi] 握手成功 [OK]")
                    if self.on_state_change:
                        self.on_state_change("idle")
                    return True
            except asyncio.TimeoutError:
                print("[Xiaozhi] hello响应超时")
        except Exception as e:
            print(f"[Xiaozhi] 连接失败: {e}")
            try:
                if self.ws:
                    await self.ws.close()
            except Exception:
                pass
            self.ws = None
        return False

    async def _msg_loop(self):
        close_info = {"code": None, "reason": "", "source": "remote"}
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
                    await asyncio.sleep(0)  # yield让字幕broadcast task先执行
                elif isinstance(msg, bytes):
                    if self.on_audio:
                        await self.on_audio(msg)  # 内联解码, 不创建任务
        except websockets.ConnectionClosed as e:
            print(f"[Xiaozhi] 连接关闭: {e}")
            close_info = {
                "code": getattr(e, "code", None),
                "reason": getattr(e, "reason", "") or "",
                "source": "websocket",
            }
        except Exception as e:
            print(f"[Xiaozhi] 错误: {e}")
            close_info = {
                "code": None,
                "reason": str(e),
                "source": "exception",
            }
        finally:
            if close_info["code"] is None and self.ws:
                close_info["code"] = getattr(self.ws, "close_code", None)
                close_info["reason"] = (
                    getattr(self.ws, "close_reason", "") or close_info["reason"]
                )
            was = self.connected
            self.connected = False
            if was and self.on_state_change:
                self.on_state_change("disconnected", close_info)

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
        self._browser_broadcast_lock = asyncio.Lock()
        self._xiaozhi_connect_lock = asyncio.Lock()
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
        self._intentional_standby = False
        self._standby_reason = ''
        self._standby_message = ''
        self._session_end_pending = False
        self._reconnect_task: asyncio.Task | None = None
        self._voice_start_time = 0.0
        self._wake_detect_time = 0.0
        self._wake_response_pcm = None  # 预缓存唤醒应答音PCM
        self._feedback_pcm = None      # 反馈提示音(100ms sine chirp)
        self._feedback_triggered = False  # 每轮对话只触发一次反馈
        self._vad_has_speech = False   # 本轮聆听是否检测到过语音
        self._intent_responses = {}   # {key: numpy PCM} 意图响应预录音频
        self._intent_texts = {       # {key: str} 意图响应字幕文字
            "timer_set":    "好的，已设置闹钟",
            "timer_cancel": "闹钟已取消",
            "timer_none":   "当前没有闹钟",
            "light_on":     "灯已打开",
            "light_off":    "灯已关闭",
            "brightness":   "亮度已调整",
            "brightness_low":  "灯光已调到低档",
            "brightness_mid":  "灯光已调到中档",
            "brightness_high": "灯光已调到高档",
            "volume_0":     "音量已静音",
            "volume_25":    "音量已调到百分之二十五",
            "volume_50":    "音量已调到百分之五十",
            "volume_75":    "音量已调到百分之七十五",
            "volume_100":   "音量已调到百分之一百",
            "dialog_exit":  "\u597d\u7684\uff0c\u5df2\u7ed3\u675f\u672c\u6b21\u5bf9\u8bdd",
        }
        self._skip_tts = False        # 已用本地音频响应, 丢弃服务端TTS
        self._local_intent_feedback_active = False
        self._subtitle_turn_id = 0
        self._active_stt_turn_id = None
        config_manager = ConfigManager.get_instance()
        self._accept_streaming_asr = bool(config_manager.get_config(
            "UI_OPTIONS.ACCEPT_STREAMING_ASR_SUBTITLE", True
        ))
        self._server_streaming_asr_available = False
        self._last_send_time = 0.0
        self._speech_start_time = 0.0   # 本轮对话首次音频发送时间
        self._pre_listen_opus = deque(maxlen=50)  # 打断前缓存的 Opus 帧(约1秒)
        self._log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")
        self._log_queue = asyncio.Queue()
        self._log_task: asyncio.Task | None = None
        self._last_xiaozhi_attempt = 0.0  # xiaozhi重连冷却时间戳

    def _log(self, msg):
        t = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{t}] {msg}\n"
        try:
            self._log_queue.put_nowait(line)
        except asyncio.QueueFull:
            pass

    async def _log_writer(self):
        while True:
            try:
                line = await asyncio.wait_for(self._log_queue.get(), timeout=5.0)
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
            except asyncio.TimeoutError:
                continue
            except Exception:
                pass

    def _setup_routes(self):
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/ws", self._handle_browser_ws)
        static_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "static"
        )
        self.app.router.add_static("/assets/", static_dir, show_index=False)

    # ========== 服务器启动 ==========
    async def start_server(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.config["local_host"], self.config["local_port"])
        await site.start()
        self._log_task = asyncio.create_task(self._log_writer())
        print(f"[Bridge] HTTP服务: http://{self.config['local_host']}:{self.config['local_port']}")
        self._log("服务启动")

    async def start_audio(self):
        """初始化硬件音频: 麦克风采集 + 扬声器播放"""
        self._load_tts_cache()  # 启动时加载上次对话的TTS缓存
        print("[Bridge] 初始化音频设备...")
        self.codec = AudioCodec()
        await self.codec.initialize()
        self.codec.set_encoded_callback(self._on_mic_opus)
        # VAD: 追踪首音时间用于延迟统计 + 静音检测触发即时反馈
        class _VAD:
            def __init__(self, bridge):
                self.bridge = bridge
                self._silence_frames = 0
                self._silence_threshold = 15  # 15帧×20ms=300ms
            def on_audio_data(self, audio_data):
                if int(np.max(np.abs(audio_data))) > VAD_THRESHOLD:
                    self.bridge._voice_start_time = time.time()
                    self.bridge._vad_has_speech = True
                    self._silence_frames = 0
                    self.bridge._feedback_triggered = False
                elif (self.bridge._device_state == DeviceState.LISTENING
                      and self.bridge._vad_has_speech
                      and not self.bridge._feedback_triggered):
                    self._silence_frames += 1
                    if self._silence_frames >= self._silence_threshold:
                        self.bridge._feedback_triggered = True
                        self.bridge._main_loop.call_soon_threadsafe(
                            lambda: asyncio.ensure_future(self.bridge._on_speech_end()))
        vad = _VAD(self)
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
        print("[Bridge] 音频设备就绪 [OK]")
        await self._preload_wake_response()

    async def start_wake_word(self):
        """初始化并启动唤醒词检测"""
        try:
            # 指向我们自己的models目录(含"灵犀台灯"唤醒词)
            config = ConfigManager.get_instance()
            config.update_config("WAKE_WORD_OPTIONS.MODEL_PATH", "models")
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
                print("[Bridge] 唤醒词检测已启动 [OK]")
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
        # 唤醒代表用户明确要求恢复会话，允许从服务端空闲断联状态重连。
        self._intentional_standby = False
        self._wake_detect_time = time.time()
        await self._broadcast_json({"type": "wake_detected", "wake_word": wake_word})
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
        await self._send_listen_start()
        await self._set_state(DeviceState.LISTENING)

    async def _on_speech_end(self):
        """用户说完话后300ms静音触发: 播放反馈提示音(已禁用)"""
        return
        if not self._feedback_triggered:
            return
        if self._device_state != DeviceState.LISTENING:
            return
        if self._feedback_pcm is not None and self.codec:
            await self.codec.write_pcm_direct(self._feedback_pcm.copy())

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
        await self._send_listen_start()
        # flush 打断前缓存的 Opus 帧, 确保开头语音不丢失
        buf = list(self._pre_listen_opus)
        self._pre_listen_opus.clear()
        for opus_data in buf:
            await self.xiaozhi.send_audio(opus_data)
        await self._set_state(DeviceState.LISTENING)

    async def connect_xiaozhi(self, report_errors=True):
        async with self._xiaozhi_connect_lock:
            if self.xiaozhi and self.xiaozhi.connected:
                return True
            self._last_xiaozhi_attempt = time.time()
            print(f"[Bridge] 连接xiaozhi: {self.config['xiaozhi_ws_url']}")
            if self.xiaozhi:
                await self.xiaozhi.close()
            self.xiaozhi = XiaozhiClient(
                self.config["xiaozhi_ws_url"], self.config["xiaozhi_token"],
                self.config["device_id"], self.config["client_id"],
            )
            self.xiaozhi.on_json = self._on_xiaozhi_json
            self.xiaozhi.on_audio = self._on_xiaozhi_audio
            self.xiaozhi.on_state_change = self._on_xiaozhi_state
            ok = await self.xiaozhi.connect()
            if ok:
                self._standby_reason = ""
                self._standby_message = ""
                self._stop_companion()
                await self._set_state(DeviceState.IDLE)
            elif report_errors:
                await self._broadcast_json({"type": "state", "state": "disconnected"})
                await self._broadcast_json({"type": "error", "message": "暂时无法连接，请稍后继续对话"})
            return ok

    async def wait_closed(self):
        await self._shutdown_event.wait()

    async def close(self):
        self._shutdown_event.set()
        self._cancel_idle_timer()
        self._cancel_reconnect_task()
        self._save_tts_cache()  # 关闭时落盘
        if self._log_task:
            self._log_task.cancel()
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
        return web.Response(text=render_index_html(), content_type="text/html")

    # ========== 浏览器 WebSocket ==========
    async def _handle_browser_ws(self, request):
        ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024, heartbeat=15.0)
        await ws.prepare(request)
        self.browser_ws_set.add(ws)
        print(f"[Bridge] 浏览器连接 (共{len(self.browser_ws_set)})")
        await ws.send_json({
            "type": "asr_streaming_setting",
            "enabled": self._accept_streaming_asr,
            "server_available": self._server_streaming_asr_available,
        })

        # 发送当前状态, 如果xiaozhi断连则自动重连
        if self.xiaozhi and self.xiaozhi.connected:
            await ws.send_json({"type": "state", "state": "idle"})
        elif self.xiaozhi and not self.xiaozhi.connected:
            if self._intentional_standby:
                await ws.send_json({"type": "state", "state": "disconnected"})
                await ws.send_json({
                    "type": "session_end",
                    "reason": self._standby_reason or "session_ended",
                    "message": self._standby_message or "本次对话已结束，可以点击继续对话",
                })
                await ws.send_json({"type": "wake_word", "enabled": self._wake_word_enabled})
                await ws.send_json({"type": "light_state", "level": self._light_level})
                try:
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            try:
                                await self._on_browser_cmd(json.loads(msg.data))
                            except json.JSONDecodeError:
                                pass
                        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                            break
                finally:
                    self.browser_ws_set.discard(ws)
                return
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
            self._intentional_standby = False
            if not self.xiaozhi or not self.xiaozhi.connected:
                await self._broadcast_json({"type": "error", "message": "xiaozhi未连接"})
                return
            self._keep_listening = True
            self._speech_start_time = 0.0  # 重置, 等首帧音频触发
            await self._send_listen_start()
            await self._set_state(DeviceState.LISTENING)
            print("[Bridge] 已发送listen start + 状态→listening")
        elif t == "stop_listening":
            self._keep_listening = False
            await self.xiaozhi.send_text(json.dumps({"type": "listen", "state": "stop"}))
            await self._set_state(DeviceState.IDLE)
        elif t == "abort":
            self._keep_listening = True
            if self.xiaozhi and self.xiaozhi.connected:
                await self.xiaozhi.send_text(json.dumps({"type": "abort"}))
            if self.codec:
                await self.codec.clear_audio_queue()
            if self.xiaozhi and self.xiaozhi.connected:
                await self._send_listen_start()
                await self._set_state(DeviceState.LISTENING)
            else:
                await self._set_state("disconnected")
        elif t == "toggle_wake_word":
            enabled = await self.toggle_wake_word()
            await self._broadcast_json({"type": "wake_word", "enabled": enabled})
        elif t == "set_streaming_asr":
            enabled = bool(data.get("enabled", True))
            saved = ConfigManager.get_instance().update_config(
                "UI_OPTIONS.ACCEPT_STREAMING_ASR_SUBTITLE", enabled
            )
            if saved:
                self._accept_streaming_asr = enabled
            await self._broadcast_json({
                "type": "asr_streaming_setting",
                "enabled": self._accept_streaming_asr,
                "server_available": self._server_streaming_asr_available,
                "saved": saved,
            })
        elif t == "set_timer":
            mins = float(data.get("minutes", 5))
            label = data.get("label", "")
            asyncio.create_task(self.start_timer(mins, label))
        elif t == "cancel_timer":
            self._do_cancel_timer()
        elif t == "light_toggle":
            self._light_level = 0 if self._light_level > 0 else 3
            await self._broadcast_json({
                "type": "light_state", "level": self._light_level
            })
        elif t == "set_brightness":
            level = int(data.get("level", 3))
            self._light_level = max(0, min(3, level))
            await self._broadcast_json({
                "type": "light_state", "level": self._light_level
            })
        elif t == "end_conversation":
            await self._enter_standby(
                "user_ended", "本次对话已结束，可以点击继续对话"
            )
        elif t == "reconnect":
            await self._request_reconnect()

    def _cancel_reconnect_task(self):
        task = self._reconnect_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self._reconnect_task = None

    async def _enter_standby(self, reason, message):
        self._cancel_reconnect_task()
        self._intentional_standby = True
        self._standby_reason = reason
        self._standby_message = message
        self._keep_listening = False
        self._cancel_idle_timer()
        if self.codec:
            await self.codec.clear_audio_queue()
        await self._broadcast_json({
            "type": "session_end", "reason": reason, "message": message,
        })
        if self.xiaozhi:
            await self.xiaozhi.close()
        await self._set_state("disconnected")

    async def _request_reconnect(self):
        self._cancel_reconnect_task()
        self._intentional_standby = True
        self._keep_listening = False
        if self.xiaozhi:
            await self.xiaozhi.close()
        self._intentional_standby = False
        await self._broadcast_json({"type": "state", "state": "connecting"})
        ok = await self.connect_xiaozhi(report_errors=False)
        if not ok:
            await self._enter_standby(
                "connection_lost", "暂时无法连接，请检查网络后继续对话"
            )

    # ========== 麦克风 → xiaozhi ==========
    async def _send_listen_start(self):
        """开始一轮拾音，并声明客户端是否接受 ASR 中间字幕。"""
        if not self.xiaozhi or not self.xiaozhi.connected:
            return False
        await self.xiaozhi.send_text(json.dumps({
            "type": "listen",
            "state": "start",
            "mode": "auto",
            "accept_partial_stt": self._accept_streaming_asr,
        }))
        return True

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
    async def _handle_asr_wakeup_response(self, data):
        """播放服务端ASR分段唤醒的本地预录音，不进入LLM/TTS链路。"""
        text = data.get("text", "你好，我在")
        await self._set_state(DeviceState.SPEAKING)
        await self._broadcast_json({"type": "tts", "state": "start", "text": text})
        await self._broadcast_json({"type": "tts", "state": "sentence_start", "text": text})
        await self._play_wake_response()
        await self._broadcast_json({"type": "tts", "state": "stop"})
        if self._keep_listening:
            asyncio.create_task(self._auto_restart())
        else:
            await self._set_state(DeviceState.IDLE)

    async def _forward_stt_to_browser(self, data, turn_id):
        """保证ASR字幕先于思考状态到达浏览器。"""
        is_final = data.get("final", True) is not False
        await self._broadcast_json({
            "type": "stt",
            "text": data.get("text", ""),
            "session_id": data.get("session_id", ""),
            "turn_id": turn_id,
            "final": is_final,
        })
        if is_final:
            await self._broadcast_json({
                "type": "state", "state": "thinking", "turn_id": turn_id,
            })

    def _on_xiaozhi_json(self, data):
        t = data.get("type", "")
        if t == "asr_wakeup_response":
            asyncio.create_task(self._handle_asr_wakeup_response(data))
        elif t == "session_end":
            reason = data.get("reason") or "server_ended"
            message = data.get("message") or (
                "长时间未交流，本次对话已结束"
                if reason == "idle_timeout"
                else "服务端已结束本次对话"
            )
            if reason == "idle_timeout":
                message = "\u957f\u65f6\u95f4\u672a\u5bf9\u8bdd\uff0c\u8fde\u63a5\u5df2\u65ad\u5f00"
            # 必须在异步清理前同步设置，避免连接先关闭而误触发自动重连。
            self._intentional_standby = True
            self._standby_reason = reason
            self._standby_message = message
            self._session_end_pending = True
            asyncio.create_task(self._handle_server_session_end(reason, message))
        elif t == "asr_capability":
            self._server_streaming_asr_available = bool(
                data.get("streaming_subtitle", False)
            )
            asyncio.create_task(self._broadcast_json({
                "type": "asr_streaming_setting",
                "enabled": self._accept_streaming_asr,
                "server_available": self._server_streaming_asr_available,
            }))
        elif t == "stt":
            is_final = data.get("final", True) is not False
            if not is_final and not self._accept_streaming_asr:
                return
            if is_final and self._speech_start_time and data.get("text"):
                asr_latency = time.time() - self._speech_start_time
                msg = f"首音→STT返回: {asr_latency:.2f}s text={data.get('text','')[:30]}"
                print(f"[Log] {msg}")
                self._log(msg)
            server_turn_id = data.get("turn_id")
            if server_turn_id is not None:
                turn_id = server_turn_id
            elif not is_final and self._active_stt_turn_id is not None:
                turn_id = self._active_stt_turn_id
            else:
                self._subtitle_turn_id += 1
                turn_id = self._subtitle_turn_id
            self._active_stt_turn_id = None if is_final else turn_id
            self._subtitle_turn_id = turn_id
            asyncio.create_task(
                self._forward_stt_to_browser(data, turn_id)
            )
        elif t == "llm":
            asyncio.create_task(self._broadcast_json({
                "type": "llm",
                "emotion": data.get("emotion", "neutral"),
            }))
        elif t == "tts":
            state = data.get("state", "")
            if self._skip_tts:
                if state == "stop":
                    self._skip_tts = False
                return
            text = data.get("text", "")
            if state == "start" and self._speech_start_time:
                tts_latency = time.time() - self._speech_start_time
                msg = f"首音→首句播报: {tts_latency:.2f}s text={text[:30]}"
                print(f"[Log] {msg}")
                self._log(msg)
            # 处理 sentence_start 里的文字 (字幕)
            if state == "sentence_start" and data.get("text"):
                asyncio.create_task(self._broadcast_json(
                    {"type": "tts", "state": state, "text": data.get("text"),
                     "session_id": data.get("session_id", ""), "turn_id": self._subtitle_turn_id}))
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
            if text and state != "sentence_start":
                asyncio.create_task(self._broadcast_json(
                    {"type": "tts", "state": state, "text": text,
                     "session_id": data.get("session_id", ""), "turn_id": self._subtitle_turn_id}))
            elif state != "start":
                asyncio.create_task(self._broadcast_json(
                    {"type": "tts", "state": state, "session_id": data.get("session_id", ""),
                     "turn_id": self._subtitle_turn_id}))
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

    async def _handle_server_session_end(self, reason, message):
        """\u5904\u7406\u670d\u52a1\u7aef\u7ed3\u675f\u4e8b\u4ef6\uff1b\u8bed\u97f3\u9000\u51fa\u5148\u64ad\u653e\u672c\u5730\u56fa\u5b9a\u53cd\u9988\uff0c\u518d\u590d\u7528\u5f85\u673a\u6d41\u7a0b\u3002"""
        try:
            if reason == "voice_exit":
                await self._play_local_intent_feedback("dialog_exit", message)
            await self._enter_standby(reason, message)
        finally:
            self._session_end_pending = False

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
            "description": "设置倒计时闹钟,到时间后提醒。参数minutes为分钟数。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "number",
                        "description": "分钟数",
                    },
                    "label": {
                        "type": "string",
                        "description": "标签,可选",
                    },
                },
                "required": ["minutes"],
            },
        },
        {
            "name": "cancel_timer",
            "description": "取消当前运行的倒计时闹钟。",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "light_toggle",
            "description": "切换台灯开关。",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "adjust_brightness",
            "description": "按当前亮度调高或调低一档。关→低→中→高，反向为高→中→低→关。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                },
                "required": ["direction"],
            },
        },
        {
            "name": "set_brightness",
            "description": "设置台灯亮度档位。0=关,1=弱,2=中,3=亮。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "integer",
                        "description": "亮度: 0关 1弱 2中 3亮",
                        "minimum": 0,
                        "maximum": 3,
                    },
                },
                "required": ["level"],
            },
        },
        {
            "name": "set_volume",
            "description": "设置台灯扬声器音量。音量只有0、25、50、75、100五档。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "percent": {
                        "type": "integer",
                        "description": "目标音量百分比，自动吸附到最近档位",
                        "minimum": 0,
                        "maximum": 100,
                    },
                },
                "required": ["percent"],
            },
        },
        {
            "name": "adjust_volume",
            "description": "把台灯扬声器音量调高一档或调低一档。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "up调高一档，down调低一档",
                    },
                },
                "required": ["direction"],
            },
        }]
        await self._mcp_reply(msg_id, {"tools": tools})

    async def _play_local_intent_feedback(self, response_key, subtitle):
        """按确定顺序发送字幕并播放内存中的本地反馈音频。"""
        subtitle = subtitle or self._intent_texts.get(response_key, "")
        self._skip_tts = True
        self._local_intent_feedback_active = True
        try:
            await self._broadcast_json({
                "type": "tts", "state": "start", "local": True,
                "turn_id": self._subtitle_turn_id,
            })
            if subtitle:
                await self._broadcast_json({
                    "type": "tts", "state": "sentence_start", "text": subtitle,
                    "local": True, "turn_id": self._subtitle_turn_id,
                })
            await self._set_state(DeviceState.SPEAKING)
            await self._play_intent_response(response_key)
        finally:
            await self._broadcast_json({
                "type": "tts", "state": "stop", "local": True,
                "turn_id": self._subtitle_turn_id,
            })
            self._local_intent_feedback_active = False
    async def _mcp_tools_call(self, msg_id, params):
        name = params.get("name", "")
        args = params.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        print(f"[Bridge] MCP tools/call: {name}, args={json.dumps(args, ensure_ascii=False)}")
        response_key = None
        if name == "set_timer":
            minutes = float(args.get("minutes", 0))
            label = args.get("label", "") or f"{int(minutes)}分钟"
            if minutes <= 0:
                await self._mcp_reply(msg_id, error_msg="分钟数必须大于0")
                return
            asyncio.create_task(self.start_timer(minutes, label))
            result_text = f"已设置{label}闹钟,{int(minutes)}分钟后提醒"
            response_key = "timer_set"
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
        elif name == "cancel_timer":
            if self._timer_task and not self._timer_task.done():
                self._timer_task.cancel()
                self._timer_task = None
                result_text = "闹钟已取消"
                response_key = "timer_cancel"
            else:
                result_text = "当前没有正在运行的闹钟"
                response_key = "timer_none"
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
            await self._broadcast_json({"type": "timer_cancelled"})
        elif name == "light_toggle":
            if self._light_level == 0:
                self._light_level = 3
                result_text = "灯已打开，全亮"
                response_key = "light_on"
            else:
                self._light_level = 0
                result_text = "灯已关闭"
                response_key = "light_off"
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
            await self._broadcast_json({"type": "light_state", "level": self._light_level})
        elif name == "adjust_brightness":
            direction = str(args.get("direction", "up")).lower()
            level = min(3, self._light_level + 1) if direction not in ("down", "lower", "decrease") else max(0, self._light_level - 1)
            self._light_level = level
            labels = {0: "灯已关闭", 1: "灯光已调到低档", 2: "灯光已调到中档", 3: "灯光已调到高档"}
            result_text = labels[level]
            response_key = {0: "light_off", 1: "brightness_low", 2: "brightness_mid", 3: "brightness_high"}[level]
            await self._mcp_reply(msg_id, {"content": [{"type": "text", "text": result_text}]})
            await self._broadcast_json({"type": "light_state", "level": self._light_level})
        
        elif name == "set_brightness":
            level = int(args.get("level", 3))
            level = max(0, min(3, level))
            self._light_level = level
            labels = {0: "灯已关闭", 1: "灯光已调到弱光", 2: "灯光已调到中等", 3: "灯光已调到全亮"}
            result_text = labels.get(level, f"亮度已设为{level}")
            response_key = {0: "light_off", 1: "brightness_low", 2: "brightness_mid", 3: "brightness_high"}[level]
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
            await self._broadcast_json({"type": "light_state", "level": self._light_level})
        elif name in ("set_volume", "adjust_volume"):
            if not self.codec:
                await self._mcp_reply(msg_id, error_msg="音频设备尚未就绪")
                return
            if name == "set_volume":
                level = self._nearest_volume_level(args.get("percent", 100))
            else:
                direction = str(args.get("direction", "up")).lower()
                level = self._next_volume_level(direction not in ("down", "lower", "decrease"))
            result_text = f"音量已调到{level}%"
            response_key = f"volume_{level}"
            await self._mcp_reply(msg_id, {
                "content": [{"type": "text", "text": result_text}],
            })
            await self._apply_volume_level(level, response_key)
            return
        else:
            await self._mcp_reply(msg_id, error_msg=f"未知工具: {name}")

        if response_key:
            subtitle = self._intent_texts.get(response_key, result_text)
            await self._play_local_intent_feedback(response_key, subtitle)
            if self._keep_listening:
                asyncio.create_task(self._auto_restart())
            else:
                await self._set_state(DeviceState.IDLE)

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
        self._try_volume_command(name, args)


    @staticmethod
    def _nearest_volume_level(value):
        try:
            value = max(0, min(100, float(value)))
        except (TypeError, ValueError):
            value = 100
        return min((0, 25, 50, 75, 100), key=lambda level: abs(level - value))

    def _next_volume_level(self, increase):
        current = self.codec.get_output_volume() if self.codec else 100
        levels = (0, 25, 50, 75, 100)
        nearest = self._nearest_volume_level(current)
        index = levels.index(nearest)
        index = min(4, index + 1) if increase else max(0, index - 1)
        return levels[index]

    async def _apply_volume_level(self, level, response_key, abort_server=False):
        if not self.codec:
            return
        level = self._nearest_volume_level(level)
        if abort_server and self.xiaozhi and self.xiaozhi.connected:
            await self.xiaozhi.send_text(json.dumps({"type": "abort"}))
        if level != 0:
            self.codec.set_output_volume(level)
        await self._play_local_intent_feedback(
            response_key, self._intent_texts.get(response_key, f"音量已调到{level}%")
        )
        if level == 0:
            self.codec.set_output_volume(0)
        if self._keep_listening:
            asyncio.create_task(self._auto_restart())
        else:
            await self._set_state(DeviceState.IDLE)


    def _try_volume_command(self, name, args):
        """function_call兜底音量控制。"""
        if not self.codec or not name:
            return
        lowered = str(name).lower()
        if not any(key in lowered for key in ("set_volume", "adjust_volume", "volume", "音量")):
            return
        if "set_volume" in lowered or any(key in args for key in ("percent", "volume", "level")):
            level = self._nearest_volume_level(args.get("percent", args.get("volume", args.get("level", 100))))
        else:
            direction = str(args.get("direction", args.get("action", name))).lower()
            level = self._next_volume_level(not any(key in direction for key in ("down", "lower", "decrease", "小", "低", "降")))
        asyncio.create_task(self._apply_volume_level(level, f"volume_{level}"))

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
            print("[Bridge] 意图识别取消闹钟 [OK]")
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
        if self._skip_tts:
            return
        if not self.codec:
            return
        try:
            # 缓存原始Opus数据(供本地复播用)
            self._current_opus_buf.append(opus_data)
            # py-xiaozhi标准播放路径
            await self.codec.write_audio(opus_data)
        except Exception as e:
            print(f"[Bridge] TTS播放异常: {e}")

    def _on_xiaozhi_state(self, state, details=None):
        if state != "disconnected":
            asyncio.create_task(self._broadcast_json({"type": "state", "state": state}))
            return
        self._cancel_idle_timer()
        if self._intentional_standby:
            if self._session_end_pending:
                return
            asyncio.create_task(self._broadcast_json({"type": "state", "state": "disconnected"}))
            return
        details = details or {}
        close_code = details.get("code")
        close_reason = details.get("reason") or ""
        if close_code in (1000, 1001):
            # 服务端正常关闭代表本次会话结束，等待用户继续或通过唤醒词恢复。
            self._intentional_standby = True
            self._standby_reason = "server_closed"
            self._standby_message = close_reason or "服务端已结束本次对话"
            asyncio.create_task(self._enter_standby(
                self._standby_reason, self._standby_message
            ))
            return
        asyncio.create_task(self._broadcast_json({"type": "state", "state": "connecting"}))
        if not self._reconnect_task or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._try_reconnect())

    async def _auto_restart(self):
        if self.codec:
            for _ in range(120):
                if self.codec._output_buffer.empty() and not self.codec._resample_output_buffer:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.15)
        if self._keep_listening and self.xiaozhi and self.xiaozhi.connected:
            await self.codec.clear_audio_queue()
            await self._send_listen_start()
            await self._set_state(DeviceState.LISTENING)

    async def _try_reconnect(self):
        resume_listening = self._keep_listening
        current_task = asyncio.current_task()
        try:
            for i in range(5):
                await asyncio.sleep(min(3 * (i + 1), 15))  # 3/6/9/12/15s退避
                if self._intentional_standby:
                    return
                if self.xiaozhi and self.xiaozhi.connected:
                    return
                if await self.connect_xiaozhi(report_errors=False):
                    if resume_listening and self._keep_listening and not self._intentional_standby:
                        await self._send_listen_start()
                        await self._set_state(DeviceState.LISTENING)
                    return
            await self._enter_standby(
                "connection_lost", "暂时无法连接，请检查网络后继续对话"
            )
        finally:
            if self._reconnect_task is current_task:
                self._reconnect_task = None

    async def _set_state(self, state):
        self._device_state = state
        await self._broadcast_json({"type": "state", "state": state})
        if state == DeviceState.IDLE:
            # 空闲只表示等待唤醒，不再由客户端30秒主动断开连接
            self._pre_listen_opus.clear()
            if self._wake_word_detector:
                self._wake_word_detector.paused = False
            if self._energy_detector:
                self._energy_detector.disable()
        elif state == DeviceState.LISTENING:
            self._cancel_idle_timer()
            self._pre_listen_opus.clear()
            self._feedback_triggered = False
            self._vad_has_speech = False
            self._skip_tts = False
            if self._wake_word_detector:
                self._wake_word_detector.paused = True
            if self._energy_detector:
                self._energy_detector.disable()
        elif state == DeviceState.SPEAKING:
            if self._wake_word_detector:
                self._wake_word_detector.paused = False
            if self._energy_detector:
                self._energy_detector.enable()  # 允许语音打断
        elif state == "disconnected":
            self._pre_listen_opus.clear()
            if self._wake_word_detector:
                self._wake_word_detector.paused = False
            if self._energy_detector:
                self._energy_detector.disable()

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
        await self._set_state(DeviceState.SPEAKING)
        await self._broadcast_json({"type": "tts", "state": "start", "text": text})
        for opus in opus_frames:
            if self.xiaozhi and self.xiaozhi.connected:
                return
            await self.codec.write_audio(opus)
        await self._broadcast_json({"type": "tts", "state": "stop"})
        await self._set_state(DeviceState.IDLE)

    # ========== 闹钟记时 ==========
    def _generate_feedback_sound(self):
        """纯numpy生成100ms上升音调(800→1200Hz), 零I/O零文件"""
        sr = AudioConfig.OUTPUT_SAMPLE_RATE
        duration = 0.1
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        freq = np.linspace(800, 1200, len(t))
        envelope = np.sin(np.pi * t / duration)  # 淡入淡出
        samples = (np.sin(2 * np.pi * freq * t) * envelope * 0.25 * 32767).astype(np.int16)
        return samples

    async def _preload_intent_responses(self):
        """预解码所有意图响应MP3为PCM, MCP工具执行后即时播放跳过服务端TTS"""
        base = os.path.dirname(os.path.abspath(__file__))
        files = {
            "timer_set":    "resp_timer_set.mp3",
            "timer_cancel": "resp_timer_cancel.mp3",
            "timer_none":   "resp_timer_none.mp3",
            "light_on":     "resp_light_on.mp3",
            "light_off":    "resp_light_off.mp3",
            "brightness":   "resp_brightness.mp3",
            "brightness_low": "resp_brightness_low.mp3",
            "brightness_mid": "resp_brightness_mid.mp3",
            "brightness_high": "resp_brightness_high.mp3",
            "volume_0":     "resp_volume_0.mp3",
            "volume_25":    "resp_volume_25.mp3",
            "volume_50":    "resp_volume_50.mp3",
            "volume_75":    "resp_volume_75.mp3",
            "volume_100":   "resp_volume_100.mp3",
            "dialog_exit":  "resp_dialog_exit.mp3",
        }
        sr = AudioConfig.OUTPUT_SAMPLE_RATE
        for key, fname in files.items():
            path = os.path.join(base, fname)
            if not os.path.exists(path):
                print(f"[Bridge] 意图响应文件缺失: {fname}")
                continue
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "quiet",
                   "-i", path, "-f", "s16le", "-acodec", "pcm_s16le",
                   "-ac", "1", "-ar", str(sr), "pipe:1"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                pcm, _ = await proc.communicate()
                if pcm:
                    self._intent_responses[key] = np.frombuffer(pcm, dtype=np.int16)
                    print(f"[Bridge] 意图响应已缓存: {key}")
            except Exception as e:
                print(f"[Bridge] 意图响应解码失败 {fname}: {e}")

    async def _play_intent_response(self, key):
        """播放预缓存的意图响应PCM音频"""
        pcm = self._intent_responses.get(key)
        if pcm is None and key in ("brightness_low", "brightness_mid", "brightness_high"):
            pcm = self._intent_responses.get("brightness")
        if pcm is None or not self.codec:
            return
        frame_size = AudioConfig.OUTPUT_FRAME_SIZE
        for i in range(0, len(pcm), frame_size):
            frame = pcm[i:i + frame_size]
            await self.codec.write_pcm_direct(frame.copy())

    async def _preload_wake_response(self):
        """启动阶段预解码唤醒应答音, 后续唤醒直接使用缓存避免FFmpeg进程开销"""
        self._feedback_pcm = self._generate_feedback_sound()
        print(f"[Bridge] 反馈音效已就绪: {len(self._feedback_pcm)}样本")
        await self._preload_intent_responses()
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
        async with self._browser_broadcast_lock:
            for ws in list(self.browser_ws_set):
                try:
                    await ws.send_json(data)
                except Exception:
                    self.browser_ws_set.discard(ws)
