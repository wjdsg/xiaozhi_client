from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid

from .paths import PROJECT_ROOT


WORKER_OUTPUT_LIMIT = 16 * 1024 * 1024


class WorkerError(RuntimeError):
    pass


class DictationWorkerClient:
    """串行、限时、可回收的内部工作进程客户端。"""

    def __init__(self, *, idle_seconds: int = 300, max_jobs: int = 50) -> None:
        self.idle_seconds = max(30, int(idle_seconds))
        self.max_jobs = max(1, int(max_jobs))
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._last_stderr: list[str] = []
        self._job_count = 0
        self._last_used = 0.0
        self._closing = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @staticmethod
    def _python_executable() -> str:
        configured = os.environ.get("DICTATION_WORKER_PYTHON", "").strip()
        if configured:
            return configured
        dedicated = PROJECT_ROOT / ".venv-dictation" / "Scripts" / "python.exe"
        return str(dedicated) if dedicated.is_file() else sys.executable

    @staticmethod
    def _dictation_options() -> dict:
        """从 config.json 读取拍照听写 OCR 的 ONNX Runtime 配置。

        返回 {"threads": int, "provider": str}。
        threads 语义：<=0 表示不限制线程（onnxruntime 自动用满核，推荐）；
        大于 0 表示手动指定 intra_op_num_threads。
        """
        try:
            from src.utils.config_manager import ConfigManager
            options = ConfigManager.get_instance().get_config("DICTATION_OPTIONS", {}) or {}
        except Exception:
            options = {}
        try:
            threads = int(options.get("ORT_INTRA_THREADS", 0))
        except (TypeError, ValueError):
            threads = 0
        provider = str(options.get("ONNX_PROVIDER", "cpu") or "cpu").strip().lower() or "cpu"
        return {"threads": threads, "provider": provider}

    async def _start_locked(self) -> None:
        if self.running:
            return
        self._last_stderr.clear()
        env = os.environ.copy()
        # The JSON-lines protocol must not inherit the Windows ANSI code page.
        # OCR/TTS payloads and progress messages contain Chinese text.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                     "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env.setdefault(name, "1")
        # 从 config.json 读取 OCR 推理线程数与执行后端，注入 OCR 工作子进程。
        # 优先级：外部环境变量 > config.json > 内置默认(自动)。
        # 仅在外部未显式设置时才按 config 注入；threads<=0 时显式移除该变量，
        # 让 onnxruntime 使用默认调度（用满核），避免历史遗留的硬编码 "2"
        # 在多核大小核机器上触发低线程数性能悬崖。
        dictation_options = self._dictation_options()
        if "DICTATION_ORT_INTRA_THREADS" not in env:
            if dictation_options["threads"] > 0:
                env["DICTATION_ORT_INTRA_THREADS"] = str(dictation_options["threads"])
            else:
                env.pop("DICTATION_ORT_INTRA_THREADS", None)
        env.setdefault("DICTATION_ONNX_PROVIDER", dictation_options["provider"])
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self._process = await asyncio.create_subprocess_exec(
            self._python_executable(), "-u", "-m", "src.dictation.worker",
            cwd=str(PROJECT_ROOT), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=WORKER_OUTPUT_LIMIT,
            **kwargs,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            ready_line = await asyncio.wait_for(self._process.stdout.readline(), timeout=12)
            ready = json.loads(ready_line.decode("utf-8")) if ready_line else {}
            if ready.get("event") != "ready":
                raise WorkerError("内部听写进程启动握手失败")
        except Exception:
            await self._terminate_locked()
            raise

    async def _drain_stderr(self) -> None:
        process = self._process
        if not process or not process.stderr:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._last_stderr.append(text)
                del self._last_stderr[:-20]

    async def request(self, action: str, payload: dict | None = None, *,
                      timeout: float = 45.0, progress=None) -> dict:
        if self._closing:
            raise WorkerError("内部听写进程正在关闭")
        async with self._lock:
            if self._job_count >= self.max_jobs:
                await self._terminate_locked()
            await self._start_locked()
            process = self._process
            request_id = uuid.uuid4().hex
            command = {"id": request_id, "action": action}
            if payload:
                command.update(payload)
            process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))
            await process.stdin.drain()
            try:
                result = await asyncio.wait_for(
                    self._read_response(request_id, progress), timeout=timeout
                )
            except asyncio.TimeoutError as exc:
                await self._terminate_locked()
                raise WorkerError(f"{action} 超过 {int(timeout)} 秒，工作进程已重启") from exc
            self._job_count += 1
            self._last_used = time.monotonic()
            self._schedule_idle_shutdown()
            return result

    async def _read_response(self, request_id: str, progress) -> dict:
        process = self._process
        while process and process.stdout:
            line = await process.stdout.readline()
            if not line:
                detail = "; ".join(self._last_stderr[-3:])
                raise WorkerError("内部听写进程意外退出" + (f"：{detail}" if detail else ""))
            # OCR runtimes can write diagnostic bytes to the inherited stdout
            # on Windows.  The worker protocol is JSON Lines, so tolerate
            # those stray lines instead of failing the photo job immediately.
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id:
                continue
            if message.get("event") == "progress":
                if progress:
                    progress(message)
                continue
            if message.get("event") == "error":
                raise WorkerError(str(message.get("error") or "内部工作进程失败"))
            if message.get("event") == "result":
                return message.get("result") or {}
        raise WorkerError("内部听写进程没有返回结果")

    def _schedule_idle_shutdown(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_shutdown())

    async def _idle_shutdown(self) -> None:
        try:
            await asyncio.sleep(self.idle_seconds)
            if time.monotonic() - self._last_used >= self.idle_seconds:
                async with self._lock:
                    await self._terminate_locked()
        except asyncio.CancelledError:
            return

    async def _terminate_locked(self) -> None:
        process, self._process = self._process, None
        self._job_count = 0
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                process.kill()
                await process.wait()
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None

    async def close(self) -> None:
        self._closing = True
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        async with self._lock:
            await self._terminate_locked()
