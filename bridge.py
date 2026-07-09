# Author: mjw
# Date: 2026-07-09
"""
bridge.py - AI学伴Web控制桥接 (AEC版)
浏览器做AEC音频采集+播放, Python只做Opus编解码+协议转发
去掉 sounddevice 依赖, 延迟与稳定性最优
"""

import asyncio
import ctypes
import json
import os
import pickle
import sys
import time
import traceback

import numpy as np
import websockets
from aiohttp import web, WSMsgType

# ==================== Opus DLL ====================
def _find_opus_dll():
    base = os.path.dirname(os.path.abspath(__file__))
    for p in [os.path.join(base, "libs", "opus.dll"),
              r"D:\qxyy\py-xiaozhi\libs\libopus\win\x64\opus.dll"]:
        if os.path.exists(p): return p
    return None

def _load_opus():
    dll = _find_opus_dll()
    if dll:
        d = os.path.dirname(dll)
        if hasattr(os, "add_dll_directory"):
            try: os.add_dll_directory(d)
            except: pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        ctypes.CDLL(dll)
        return True
    return False

_load_opus()
import opuslib

# ==================== 常量 ====================
SR_IN = 16000
SR_OUT = 24000
FRAME_DUR = 20
FS_IN = int(SR_IN * FRAME_DUR / 1000)    # 320
FS_OUT = int(SR_OUT * FRAME_DUR / 1000)  # 480
BYTES_IN = FS_IN * 2   # 640
BYTES_OUT = FS_OUT * 2 # 960

# ==================== Xiaozhi客户端 ====================
class XiaozhiClient:
    def __init__(self, url, token, device_id, client_id):
        self.url = url
        self.headers = {"Authorization": f"Bearer {token}", "Protocol-Version": "1",
                        "Device-Id": device_id, "Client-Id": client_id}
        self.ws = None; self.connected = False
        self.on_json = None; self.on_audio = None; self.on_state_change = None
        self._msg_task = None

    async def connect(self):
        import ssl
        ssl_ctx = ssl._create_unverified_context() if self.url.startswith("wss://") else None
        try:
            self.ws = await websockets.connect(
                self.url, ssl=ssl_ctx, additional_headers=self.headers,
                ping_interval=20, ping_timeout=20, close_timeout=10, max_size=10*1024*1024)
        except TypeError:
            self.ws = await websockets.connect(
                self.url, ssl=ssl_ctx, extra_headers=self.headers,
                ping_interval=20, ping_timeout=20, close_timeout=10, max_size=10*1024*1024)

        hello = {"type": "hello", "version": 1, "features": {"mcp": True},
                 "transport": "websocket",
                 "audio_params": {"format": "opus", "sample_rate": SR_IN,
                                  "channels": 1, "frame_duration": FRAME_DUR}}
        await self.ws.send(json.dumps(hello))
        print("[Xiaozhi] hello已发送")

        try:
            resp = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            data = json.loads(resp)
            if data.get("type") == "hello":
                self.connected = True
                self._msg_task = asyncio.create_task(self._msg_loop())
                print("[Xiaozhi] 握手成功")
                if self.on_state_change: self.on_state_change("idle")
                return True
        except asyncio.TimeoutError:
            print("[Xiaozhi] hello超时")
        return False

    async def _msg_loop(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, str):
                    data = json.loads(msg)
                    t = data.get("type", "?")
                    if t in ("stt", "tts", "llm"):
                        print(f"[Xiaozhi] <- {t}: {json.dumps(data, ensure_ascii=False)[:120]}")
                    if self.on_json: self.on_json(data)
                elif isinstance(msg, bytes):
                    if self.on_audio: await self.on_audio(msg)
        except websockets.ConnectionClosed as e:
            print(f"[Xiaozhi] 关闭: {e}")
        except Exception as e:
            print(f"[Xiaozhi] 异常: {e}")
        finally:
            was = self.connected; self.connected = False
            if was and self.on_state_change: self.on_state_change("disconnected")

    async def send_text(self, t): 
        if self.ws and self.connected: await self.ws.send(t)
    async def send_audio(self, d):
        if self.ws and self.connected: await self.ws.send(d)
    async def close(self):
        self.connected = False
        if self._msg_task: self._msg_task.cancel()
        if self.ws:
            try: await self.ws.close()
            except: pass


# ==================== WebBridge ====================
class WebBridge:
    def __init__(self, config):
        self.config = config
        self.xiaozhi = None
        self.browser_set = set()
        self.app = web.Application()
        self._setup_routes()
        self._shutdown_ev = asyncio.Event()
        self._opus_enc = opuslib.Encoder(SR_IN, 1, opuslib.APPLICATION_VOIP)
        self._opus_dec = opuslib.Decoder(SR_OUT, 1)

        self._keep_listen = False
        self._state = "idle"  # idle/listening/speaking
        self._idle_timer = None
        self._companion_task = None

        self._tts_cache = []
        self._cur_tts_buf = []
        self._cur_tts_text = ""
        self._click_time = 0.0

        self._pcm_queue = asyncio.Queue(maxsize=300)
        self._send_task = None

    def _setup_routes(self):
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/ws", self._handle_ws)

    # ===== 生命周期 =====
    async def start_server(self):
        r = web.AppRunner(self.app); await r.setup()
        s = web.TCPSite(r, self.config["local_host"], self.config["local_port"]); await s.start()
        print(f"[Bridge] HTTP: http://{self.config['local_host']}:{self.config['local_port']}")

    async def connect_xiaozhi(self):
        print(f"[Bridge] 连接: {self.config['xiaozhi_ws_url']}")
        self.xiaozhi = XiaozhiClient(self.config["xiaozhi_ws_url"], self.config["xiaozhi_token"],
                                      self.config["device_id"], self.config["client_id"])
        self.xiaozhi.on_json = self._on_xz_json
        self.xiaozhi.on_audio = self._on_xz_audio
        self.xiaozhi.on_state_change = self._on_xz_state
        ok = await self.xiaozhi.connect()
        if ok:
            await self._broadcast({"type": "state", "state": "idle"})
        else:
            await self._broadcast({"type": "state", "state": "disconnected"})
        return ok

    async def wait_closed(self): await self._shutdown_ev.wait()

    async def close(self):
        self._shutdown_ev.set(); self._cancel_idle(); self._stop_companion()
        if self._send_task: self._send_task.cancel()
        self._save_cache()
        if self.xiaozhi: await self.xiaozhi.close()
        for w in list(self.browser_set):
            try: await w.close()
            except: pass

    # ===== HTTP =====
    async def _handle_index(self, req):
        return web.FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))

    # ===== 浏览器WS =====
    async def _handle_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=10*1024*1024, heartbeat=15.0)
        await ws.prepare(req); self.browser_set.add(ws)

        if self.xiaozhi and self.xiaozhi.connected:
            await ws.send_json({"type": "state", "state": "idle"})
        elif self.xiaozhi:
            self._stop_companion()
            ok = await self.connect_xiaozhi()
            await ws.send_json({"type": "state", "state": "idle" if ok else "disconnected"})
        else:
            await ws.send_json({"type": "state", "state": "connecting"})

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try: await self._on_cmd(json.loads(msg.data))
                    except: pass
                elif msg.type == WSMsgType.BINARY:
                    await self._on_pcm(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED): break
        except ConnectionResetError: pass
        finally:
            self.browser_set.discard(ws)
        return ws

    # ===== 浏览器命令 =====
    async def _on_cmd(self, d):
        t = d.get("type", "")
        if t == "start_listening":
            if not self.xiaozhi or not self.xiaozhi.connected:
                await self._broadcast({"type": "error", "message": "未连接"})
                return
            self._keep_listen = True
            self._click_time = time.time()
            await self.xiaozhi.send_text(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
            self._set_state("listening")
            self._ensure_sender()
            await self._broadcast({"type": "state", "state": "listening"})
        elif t == "stop_listening":
            self._keep_listen = False
            await self.xiaozhi.send_text(json.dumps({"type": "listen", "state": "stop"}))
            self._set_state("idle")
        elif t == "abort":
            self._keep_listen = False
            await self.xiaozhi.send_text(json.dumps({"type": "abort"}))
            self._set_state("idle")
        elif t == "set_timer":
            m = float(d.get("minutes", 5)); lb = d.get("label", "")
            asyncio.create_task(self._do_timer(m, lb))
        elif t == "reconnect":
            self._keep_listen = False
            if self.xiaozhi: await self.xiaozhi.close()
            self._stop_companion()
            await self.connect_xiaozhi()

    # ===== 浏览器PCM → Opus → xiaozhi =====
    async def _on_pcm(self, data):
        if self._state != "listening": return
        try: self._pcm_queue.put_nowait(data)
        except asyncio.QueueFull: pass

    def _ensure_sender(self):
        if self._send_task and not self._send_task.done(): return
        self._send_task = asyncio.create_task(self._sender_loop())

    async def _sender_loop(self):
        buf = b""; n = 0
        while self._keep_listen and not self._shutdown_ev.is_set():
            try:
                data = await asyncio.wait_for(self._pcm_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if not self._keep_listen: break
                continue
            if self._state != "listening": continue
            buf += data
            while len(buf) >= BYTES_IN:
                chunk = buf[:BYTES_IN]; buf = buf[BYTES_IN:]
                try:
                    opus = self._opus_enc.encode(np.frombuffer(chunk, np.int16).tobytes(), FS_IN)
                    if opus and self.xiaozhi and self.xiaozhi.connected:
                        await self.xiaozhi.send_audio(opus)
                        n += 1
                except: pass

    # ===== xiaozhi回调 =====
    def _on_xz_json(self, data):
        t = data.get("type", "")
        if t == "stt":
            txt = data.get("text", "")
            if txt and self._click_time:
                print(f"[Latency] 点击→首字识别: {time.time()-self._click_time:.2f}s")
            asyncio.create_task(self._broadcast({"type": "stt", "text": txt}))
        elif t == "llm":
            asyncio.create_task(self._broadcast({"type": "llm", "emotion": data.get("emotion","neutral")}))
        elif t == "tts":
            state = data.get("state", ""); txt = data.get("text", "")
            if state == "start":
                self._cur_tts_buf = []; self._cur_tts_text = txt or ""
                self._set_state("speaking")
                if self._click_time:
                    print(f"[Latency] 点击→首句播报: {time.time()-self._click_time:.2f}s")
            if txt:
                asyncio.create_task(self._broadcast({"type": "tts", "state": state, "text": txt}))
            elif state != "start":
                asyncio.create_task(self._broadcast({"type": "tts", "state": state}))
            if state == "stop":
                if self._cur_tts_buf and self._cur_tts_text:
                    self._tts_cache.append((self._cur_tts_text, list(self._cur_tts_buf)))
                    if len(self._tts_cache) > 10: self._tts_cache = self._tts_cache[-10:]
                    self._cur_tts_buf = []
                if self._keep_listen:
                    asyncio.create_task(self._auto_restart())
        elif t == "mcp":
            self._handle_mcp(data)
        elif t == "function_call":
            self._handle_fc(data)

    async def _on_xz_audio(self, opus):
        try:
            pcm = self._opus_dec.decode(opus, FS_OUT)
            arr = np.frombuffer(pcm, dtype=np.int16)
            if self._keep_listen: self._cur_tts_buf.append(arr.copy())
            # 转发PCM到浏览器
            for w in list(self.browser_set):
                try: await w.send_bytes(pcm)
                except: self.browser_set.discard(w)
        except: pass

    def _on_xz_state(self, s):
        asyncio.create_task(self._broadcast({"type": "state", "state": s}))
        if s == "disconnected":
            self._cancel_idle()
            asyncio.create_task(self._try_reconnect())

    async def _auto_restart(self):
        await asyncio.sleep(0.5)
        if self._keep_listen and self.xiaozhi and self.xiaozhi.connected:
            await self.xiaozhi.send_text(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
            self._set_state("listening")

    async def _try_reconnect(self):
        for _ in range(5):
            await asyncio.sleep(3)
            if self.xiaozhi and await self.xiaozhi.connect():
                await self._broadcast({"type": "state", "state": "idle"}); return
        await self._broadcast({"type": "error", "message": "连接失败"})

    # ===== MCP/函数调用 =====
    def _handle_mcp(self, d):
        pl = d.get("payload", {}); m = pl.get("method","") or d.get("method","")
        p = pl.get("params",{}) or d.get("params",{})
        nm = p.get("name","") or pl.get("name","")
        args = p.get("arguments",{}) or pl.get("arguments",{})
        if isinstance(args, str):
            try: args = json.loads(args)
            except: args = {}
        self._try_timer(nm, args)

    def _handle_fc(self, d):
        nm = d.get("name","") or d.get("function_name","")
        args = d.get("arguments","{}")
        if isinstance(args, str):
            try: args = json.loads(args)
            except: args = {}
        self._try_timer(nm, args)

    def _try_timer(self, name, args):
        kw = ("set_timer","start_timer","timer","alarm","闹钟","计时","倒计时","定时")
        if not name or not any(k in str(name).lower() for k in kw): return
        m = args.get("minutes",0) or args.get("duration",0) or float(args.get("seconds",0))/60
        if isinstance(m, str):
            try: m = float(m)
            except: return
        if m <= 0: return
        lb = args.get("label","") or args.get("name","") or f"{int(m)}分钟"
        asyncio.create_task(self._do_timer(m, lb))

    # ===== 状态机 =====
    def _set_state(self, s):
        self._state = s
        if s == "idle": self._start_idle()
        elif s == "listening": self._cancel_idle()

    def _start_idle(self):
        self._cancel_idle()
        async def _t():
            await asyncio.sleep(30)
            if self._state == "idle":
                print("[Bridge] 30s空闲, 断连")
                self._keep_listen = False
                if self.xiaozhi: await self.xiaozhi.close()
                self._save_cache()
                await self._broadcast({"type": "state", "state": "disconnected"})
                self._start_companion()
        self._idle_timer = asyncio.create_task(_t())

    def _cancel_idle(self):
        if self._idle_timer and not self._idle_timer.done(): self._idle_timer.cancel()
        self._idle_timer = None

    # ===== TTS缓存 =====
    @property
    def _cache_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_cache.pkl")

    def _save_cache(self):
        if not self._tts_cache: return
        import threading
        cp = list(self._tts_cache); p = self._cache_path
        threading.Thread(target=lambda: pickle.dump(cp, open(p,"wb")), daemon=True).start()

    def _load_cache(self):
        try:
            p = self._cache_path
            if os.path.exists(p): self._tts_cache = pickle.load(open(p,"rb"))
        except: pass

    # ===== 本地陪伴 =====
    def _start_companion(self):
        if self._companion_task and not self._companion_task.done(): return
        if not self._tts_cache: return
        self._load_cache()
        if not self._tts_cache: return
        self._companion_task = asyncio.create_task(self._companion_loop())

    def _stop_companion(self):
        if self._companion_task and not self._companion_task.done(): self._companion_task.cancel()
        self._companion_task = None

    async def _companion_loop(self):
        if not self._tts_cache: return
        txt, frames = self._tts_cache[-1]
        await self._broadcast({"type": "state", "state": "speaking"})
        await self._broadcast({"type": "tts", "state": "start", "text": txt})
        for f in frames:
            if self.xiaozhi and self.xiaozhi.connected: return
            for w in list(self.browser_set):
                try: await w.send_bytes(f.tobytes())
                except: self.browser_set.discard(w)
            await asyncio.sleep(0.02)
        await self._broadcast({"type": "tts", "state": "stop"})
        await self._broadcast({"type": "state", "state": "idle"})

    # ===== 计时器 =====
    async def _do_timer(self, minutes, label=""):
        secs = int(minutes * 60); lb = label or f"{int(minutes)}分钟"
        await self._broadcast({"type": "timer_start", "seconds": secs, "label": lb})
        while secs > 0 and self._state == "idle":
            await asyncio.sleep(1); secs -= 1
        if secs <= 0:
            if self._tts_cache:
                txt, frames = self._tts_cache[-1]
                await self._broadcast({"type": "timer_done", "label": lb})
                await self._broadcast({"type": "state", "state": "speaking"})
                for f in frames:
                    for w in list(self.browser_set):
                        try: await w.send_bytes(f.tobytes())
                        except: self.browser_set.discard(w)
                    await asyncio.sleep(0.02)
                await self._broadcast({"type": "state", "state": "idle"})
            else:
                await self._broadcast({"type": "timer_done", "label": lb})

    # ===== 广播 =====
    async def _broadcast(self, data):
        for w in list(self.browser_set):
            try: await w.send_json(data)
            except: self.browser_set.discard(w)
