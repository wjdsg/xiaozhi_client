# Author: mjw
# Date: 2026-07-09
"""bridge.py - AI学伴AEC版 (精简内联)"""

import asyncio, ctypes, json, os, pickle, sys, time, traceback
import numpy as np
import websockets
from aiohttp import web, WSMsgType

# Opus DLL
def _find_opus():
    for p in [os.path.join(os.path.dirname(__file__),"libs","opus.dll"),
              r"D:\qxyy\py-xiaozhi\libs\libopus\win\x64\opus.dll"]:
        if os.path.exists(p): return p
    return None
_dll = _find_opus()
if _dll:
    if hasattr(os,"add_dll_directory"):
        try: os.add_dll_directory(os.path.dirname(_dll))
        except: pass
    os.environ["PATH"] = os.path.dirname(_dll) + os.pathsep + os.environ.get("PATH","")
    ctypes.CDLL(_dll)
import opuslib

SR_IN=16000; SR_OUT=24000; FS_IN=320; FS_OUT=480; B_IN=640; B_OUT=960

class XiaozhiClient:
    def __init__(self,url,token,did,cid):
        self.url=url; self.headers={"Authorization":f"Bearer {token}","Protocol-Version":"1","Device-Id":did,"Client-Id":cid}
        self.ws=None; self.connected=False; self.on_json=None; self.on_audio=None; self.on_state=None; self._t=None

    async def connect(self):
        import ssl; ctx=ssl._create_unverified_context() if self.url.startswith("wss://") else None
        try: self.ws=await websockets.connect(self.url,ssl=ctx,additional_headers=self.headers,ping_interval=20,ping_timeout=20,close_timeout=10,max_size=10*1024*1024)
        except TypeError: self.ws=await websockets.connect(self.url,ssl=ctx,extra_headers=self.headers,ping_interval=20,ping_timeout=20,close_timeout=10,max_size=10*1024*1024)
        await self.ws.send(json.dumps({"type":"hello","version":1,"features":{"mcp":True},"transport":"websocket","audio_params":{"format":"opus","sample_rate":SR_IN,"channels":1,"frame_duration":20}}))
        try:
            r=await asyncio.wait_for(self.ws.recv(),10.0)
            if json.loads(r).get("type")=="hello":
                self.connected=True; self._t=asyncio.create_task(self._loop())
                if self.on_state: self.on_state("idle")
                return True
        except: pass
        return False

    async def _loop(self):
        try:
            async for m in self.ws:
                if isinstance(m,str):
                    d=json.loads(m)
                    if self.on_json: self.on_json(d)
                elif isinstance(m,bytes):
                    if self.on_audio: await self.on_audio(m)
        except: pass
        finally:
            was=self.connected; self.connected=False
            if was and self.on_state: self.on_state("disconnected")

    async def send_text(self,t):
        if self.ws and self.connected:
            try: await self.ws.send(t)
            except: pass
    async def send_audio(self,d):
        if self.ws and self.connected:
            try: await self.ws.send(d)
            except: pass
    async def close(self):
        self.connected=False
        if self._t: self._t.cancel()
        if self.ws:
            try: await self.ws.close()
            except: pass

class WebBridge:
    def __init__(self,cfg):
        self.cfg=cfg; self.xz=None; self.bws=set(); self.app=web.Application()
        self._setup(); self._ev=asyncio.Event()
        self.enc=opuslib.Encoder(SR_IN,1,opuslib.APPLICATION_VOIP)
        self.dec=opuslib.Decoder(SR_OUT,1)
        self.kl=False; self._st="idle"; self._it=None; self._ct=None
        self._cache=[]; self._cb=[]; self._ctx=""; self._click=0.0
        self._obuf=b""; self._ibuf=b""

    def _setup(self):
        self.app.router.add_get("/",lambda r:web.FileResponse(os.path.join(os.path.dirname(__file__),"static","index.html")))
        self.app.router.add_get("/ws",self._ws)

    async def start(self):
        r=web.AppRunner(self.app); await r.setup()
        await web.TCPSite(r,self.cfg["host"],self.cfg["port"]).start()
        print(f"[Bridge] http://{self.cfg['host']}:{self.cfg['port']}")

    async def connect_xz(self):
        self.xz=XiaozhiClient(self.cfg["xz_url"],self.cfg["xz_token"],self.cfg["did"],self.cfg["cid"])
        self.xz.on_json=self._j; self.xz.on_audio=self._a; self.xz.on_state=self._s
        ok=await self.xz.connect()
        if ok: await self._bc({"type":"state","state":"idle"})
        else: await self._bc({"type":"state","state":"disconnected"})
        return ok

    async def wait(self): await self._ev.wait()

    async def close(self):
        self._ev.set(); self._cancel_it(); self._stop_ct()
        self._save_cache()
        if self.xz: await self.xz.close()
        for w in list(self.bws):
            try: await w.close()
            except: pass

    async def _ws(self,req):
        ws=web.WebSocketResponse(max_msg_size=10*1024*1024,heartbeat=15.0)
        await ws.prepare(req); self.bws.add(ws)

        if self.xz and self.xz.connected: await ws.send_json({"type":"state","state":"idle"})
        elif self.xz:
            self._stop_ct()
            ok=await self.connect_xz()
            await ws.send_json({"type":"state","state":"idle" if ok else "disconnected"})
        else: await ws.send_json({"type":"state","state":"connecting"})

        try:
            async for msg in ws:
                if msg.type==WSMsgType.TEXT:
                    try: await self._cmd(json.loads(msg.data))
                    except: pass
                elif msg.type==WSMsgType.BINARY:
                    self._pcm(msg.data)
                elif msg.type in(WSMsgType.ERROR,WSMsgType.CLOSED): break
        except ConnectionResetError: pass
        finally: self.bws.discard(ws)
        return ws

    async def _cmd(self,d):
        t=d.get("type","")
        if t=="start_listening":
            if not self.xz or not self.xz.connected: return await self._bc({"type":"error","message":"未连接"})
            self.kl=True; self._click=time.time(); self._ibuf=b""
            await self.xz.send_text(json.dumps({"type":"listen","state":"start","mode":"auto"}))
            self._set("listening"); await self._bc({"type":"state","state":"listening"})
        elif t=="stop_listening":
            self.kl=False; await self.xz.send_text(json.dumps({"type":"listen","state":"stop"}))
            self._set("idle")
        elif t=="abort":
            self.kl=False; await self.xz.send_text(json.dumps({"type":"abort"}))
            self._set("idle")
        elif t=="set_timer":
            asyncio.create_task(self._timer(float(d.get("minutes",5)),d.get("label","")))
        elif t=="reconnect":
            self.kl=False
            if self.xz: await self.xz.close()
            self._stop_ct(); await self.connect_xz()

    # 浏览器PCM→Opus→xiaozhi (内联, 不创建队列/协程)
    def _pcm(self,data):
        if self._st!="listening" or not self.kl: return
        try:
            self._ibuf+=data
            while len(self._ibuf)>=B_IN:
                c=self._ibuf[:B_IN]; self._ibuf=self._ibuf[B_IN:]
                op=self.enc.encode(np.frombuffer(c,np.int16).tobytes(),FS_IN)
                if op and self.xz:
                    asyncio.create_task(self.xz.send_audio(op))
        except: pass

    # xiaozhi回调
    def _j(self,d):
        t=d.get("type","")
        if t=="stt":
            txt=d.get("text","")
            if txt and self._click: print(f"[Latency] click->stt: {time.time()-self._click:.2f}s")
            asyncio.create_task(self._bc({"type":"stt","text":txt}))
        elif t=="llm":
            asyncio.create_task(self._bc({"type":"llm","emotion":d.get("emotion","neutral")}))
        elif t=="tts":
            st=d.get("state",""); txt=d.get("text","")
            if st=="start":
                self._cb=[]; self._ctx=txt or ""; self._set("speaking")
                if self._click: print(f"[Latency] click->tts: {time.time()-self._click:.2f}s")
            if txt: asyncio.create_task(self._bc({"type":"tts","state":st,"text":txt}))
            else: asyncio.create_task(self._bc({"type":"tts","state":st}))
            if st=="stop":
                self._flush()
                if self._cb and self._ctx:
                    self._cache.append((self._ctx,list(self._cb)))
                    if len(self._cache)>10: self._cache=self._cache[-10:]
                    self._cb=[]
                if self.kl: asyncio.create_task(self._restart())
        elif t=="mcp": asyncio.create_task(self._mcp(d))
        elif t=="function_call": asyncio.create_task(self._fc(d))

    async def _a(self,opus):
        try:
            pcm=self.dec.decode(opus,FS_OUT)
            if self.kl: self._cb.append(np.frombuffer(pcm,np.int16).copy())
            self._obuf+=pcm
            if len(self._obuf)>=B_OUT*10:
                for w in list(self.bws):
                    try: await w.send_bytes(self._obuf)
                    except: self.bws.discard(w)
                self._obuf=b""
        except: pass

    def _s(self,s):
        asyncio.create_task(self._bc({"type":"state","state":s}))
        if s=="disconnected": self._cancel_it(); asyncio.create_task(self._recon())

    async def _restart(self):
        await asyncio.sleep(0.5)
        if self.kl and self.xz and self.xz.connected:
            await self.xz.send_text(json.dumps({"type":"listen","state":"start","mode":"auto"}))
            self._set("listening")

    async def _recon(self):
        for _ in range(5):
            await asyncio.sleep(3)
            if self.xz and await self.xz.connect():
                await self._bc({"type":"state","state":"idle"}); return
        await self._bc({"type":"error","message":"连接失败"})

    async def _mcp(self,d):
        pl=d.get("payload",{});nm=pl.get("name","") or d.get("name","")
        args=pl.get("arguments",{}) or d.get("arguments",{})
        if isinstance(args,str):
            try: args=json.loads(args)
            except: args={}
        await self._try_timer(nm,args)

    async def _fc(self,d):
        nm=d.get("name","") or d.get("function_name","")
        args=d.get("arguments","{}")
        if isinstance(args,str):
            try: args=json.loads(args)
            except: args={}
        await self._try_timer(nm,args)

    async def _try_timer(self,name,args):
        kw=("set_timer","start_timer","timer","alarm","闹钟","计时","倒计时","定时")
        if not name or not any(k in str(name).lower() for k in kw): return
        m=args.get("minutes",0) or args.get("duration",0) or float(args.get("seconds",0))/60
        if isinstance(m,str):
            try: m=float(m)
            except: return
        if m<=0: return
        lb=args.get("label","") or args.get("name","") or f"{int(m)}分钟"
        asyncio.create_task(self._timer(m,lb))

    # 状态
    def _set(self,s):
        self._st=s
        if s=="idle": self._start_it()
        elif s=="listening": self._cancel_it()

    def _start_it(self):
        self._cancel_it()
        async def _t():
            await asyncio.sleep(30)
            if self._st=="idle":
                self.kl=False
                if self.xz: await self.xz.close()
                self._save_cache()
                await self._bc({"type":"state","state":"disconnected"})
                self._start_ct()
        self._it=asyncio.create_task(_t())

    def _cancel_it(self):
        if self._it and not self._it.done(): self._it.cancel()
        self._it=None

    # TTS缓存
    @property
    def _cp(self): return os.path.join(os.path.dirname(os.path.abspath(__file__)),"tts_cache.pkl")
    def _save_cache(self):
        if not self._cache: return
        import threading
        cp=list(self._cache); p=self._cp
        threading.Thread(target=lambda: pickle.dump(cp,open(p,"wb")),daemon=True).start()
    def _load_cache(self):
        try:
            if os.path.exists(self._cp): self._cache=pickle.load(open(self._cp,"rb"))
        except: pass

    # 陪伴
    def _start_ct(self):
        if self._ct and not self._ct.done(): return
        if not self._cache:
            self._load_cache()
            if not self._cache: return
        self._ct=asyncio.create_task(self._companion())
    def _stop_ct(self):
        if self._ct and not self._ct.done(): self._ct.cancel()
        self._ct=None
    async def _companion(self):
        if not self._cache: return
        txt,frames=self._cache[-1]
        await self._bc({"type":"state","state":"speaking"})
        await self._bc({"type":"tts","state":"start","text":txt})
        for f in frames:
            if self.xz and self.xz.connected: return
            for w in list(self.bws):
                try: await w.send_bytes(f.tobytes())
                except: self.bws.discard(w)
            await asyncio.sleep(0.02)
        await self._bc({"type":"tts","state":"stop"})
        await self._bc({"type":"state","state":"idle"})

    # 计时器
    async def _timer(self,minutes,label=""):
        secs=int(minutes*60); lb=label or f"{int(minutes)}分钟"
        await self._bc({"type":"timer_start","seconds":secs,"label":lb})
        while secs>0 and self._st=="idle":
            await asyncio.sleep(1); secs-=1
        if secs<=0:
            if self._cache:
                txt,frames=self._cache[-1]
                await self._bc({"type":"timer_done","label":lb})
                await self._bc({"type":"state","state":"speaking"})
                for f in frames:
                    for w in list(self.bws):
                        try: await w.send_bytes(f.tobytes())
                        except: self.bws.discard(w)
                    await asyncio.sleep(0.02)
                await self._bc({"type":"state","state":"idle"})
            else: await self._bc({"type":"timer_done","label":lb})

    # 杂项
    def _flush(self):
        if self._obuf:
            out=self._obuf; self._obuf=b""
            asyncio.create_task(self._flush_out(out))
    async def _flush_out(self,data):
        for w in list(self.bws):
            try: await w.send_bytes(data)
            except: self.bws.discard(w)
    async def _bc(self,data):
        for w in list(self.bws):
            try: await w.send_json(data)
            except: self.bws.discard(w)
