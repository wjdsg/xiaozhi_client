"""内部听写工作进程。

仅通过 stdin/stdout 的 JSON Lines 与主进程通信，不监听任何端口。重型
OpenCV/ONNX 与阻塞式 TTS 只在这个进程中导入，避免影响实时音频进程。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import traceback


# Keep the internal protocol deterministic even when the worker is launched
# from a GBK/ANSI Windows console rather than through DictationWorkerClient.
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8", errors="strict")
    except (AttributeError, OSError):
        pass


for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_name, "1")
os.environ.setdefault("DICTATION_ORT_INTRA_THREADS", "2")
os.environ.setdefault("DICTATION_ONNX_PROVIDER", "cpu")

from .paths import SOURCES_DIR, TIP_WORD_ROOT, TTS_CACHE_DIR, ensure_runtime_dirs


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class WorkerRuntime:
    def __init__(self) -> None:
        self._ocr = None
        self._tts = None

    def _get_ocr(self):
        if self._ocr is None:
            import cv2
            cv2.setNumThreads(1)
            from .ocr_extractor import DictationOcr
            self._ocr = DictationOcr(
                TIP_WORD_ROOT,
                max_input_side=1800,
                max_text_boxes=100,
                jieba_dict_path=SOURCES_DIR / "jieba_dict.txt",
            )
        return self._ocr

    def _get_tts(self):
        if self._tts is None:
            from .tts_provider import (
                DashScopeCosyVoiceProvider,
                TtsService,
                WindowsSapiProvider,
            )
            self._tts = TtsService(
                TTS_CACHE_DIR,
                DashScopeCosyVoiceProvider(),
                fallback_provider=WindowsSapiProvider(),
            )
        return self._tts

    def handle(self, command: dict) -> dict:
        action = command.get("action")
        if action == "ping":
            return {
                "ok": True,
                "pid": os.getpid(),
                "ocrLoaded": self._ocr is not None,
                "stdinEncoding": sys.stdin.encoding,
                "stdoutEncoding": sys.stdout.encoding,
            }
        if action == "warmup":
            return self._get_ocr().warmup()
        if action == "ocr":
            return self._ocr_extract(command)
        if action == "tts":
            return self._tts_synthesize(command)
        if action == "shutdown":
            return {"ok": True, "shutdown": True}
        raise ValueError(f"unsupported worker action: {action}")

    def _ocr_extract(self, command: dict) -> dict:
        import cv2

        image_path = Path(str(command.get("imagePath") or "")).resolve()
        if not image_path.is_file():
            raise ValueError("OCR 临时图片不存在")
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("图片无法解码")
        crop = command.get("crop")
        if isinstance(crop, dict):
            height, width = image.shape[:2]
            x = max(0.0, min(1.0, float(crop.get("x", 0))))
            y = max(0.0, min(1.0, float(crop.get("y", 0))))
            right = max(x, min(1.0, x + float(crop.get("w", 1))))
            bottom = max(y, min(1.0, y + float(crop.get("h", 1))))
            left_px, top_px = round(x * width), round(y * height)
            right_px, bottom_px = round(right * width), round(bottom * height)
            image = image[top_px:max(top_px + 1, bottom_px),
                          left_px:max(left_px + 1, right_px)]

        request_id = str(command.get("id", ""))

        def progress(phase, percent, message, details):
            _emit({"id": request_id, "event": "progress", "phase": phase,
                   "progress": percent, "message": message, "details": details})

        return self._get_ocr().extract(image, str(command.get("mode") or ""), progress=progress)

    def _tts_synthesize(self, command: dict) -> dict:
        from .tts_provider import TtsRequest

        payload = command.get("payload") or {}
        started = time.perf_counter()
        result = self._get_tts().synthesize(
            TtsRequest(
                text=str(payload.get("text") or ""),
                language=str(payload.get("language") or "zh-CN"),
                voice=str(payload.get("voice") or "longxiaochun_v2"),
                rate=payload.get("rate", 1.0),
            ),
            preferred_provider=str(payload.get("preferred_provider") or "") or None,
        )
        return {
            "status": result.status,
            "cached": result.cached,
            "reason": result.reason,
            "provider": result.provider,
            "filename": result.path.name if result.path else None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }


def main() -> int:
    ensure_runtime_dirs()
    runtime = WorkerRuntime()
    _emit({"event": "ready", "pid": os.getpid()})
    for raw_line in sys.stdin:
        try:
            command = json.loads(raw_line)
            request_id = str(command.get("id") or "")
            result = runtime.handle(command)
            _emit({"id": request_id, "event": "result", "result": result})
            if result.get("shutdown"):
                return 0
        except Exception as exc:
            _emit({
                "id": str(locals().get("command", {}).get("id") or ""),
                "event": "error",
                "error": str(exc),
                "trace": traceback.format_exc(limit=8),
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
