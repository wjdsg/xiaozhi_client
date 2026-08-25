"""Cache-first, optional DashScope TTS for low-resource devices."""

from __future__ import annotations

import hashlib
import json
import os
import re
import base64
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

AbortCheck = Callable[[], bool]
_SAFE_VOICE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


@dataclass(frozen=True)
class TtsRequest:
    text: str
    language: str = "zh-CN"
    voice: str = "longxiaochun_v2"
    rate: float = 1.0

    def normalized(self) -> "TtsRequest":
        text = " ".join(self.text.strip().split())
        language = self.language.strip() or "zh-CN"
        voice = self.voice.strip() or "longxiaochun_v2"
        rate = round(float(self.rate), 2)
        if not text:
            raise ValueError("text is required")
        if len(text) > 200:
            raise ValueError("text is too long (maximum 200 characters)")
        if not _SAFE_VOICE.fullmatch(voice):
            raise ValueError("invalid voice")
        if not 0.5 <= rate <= 2.0:
            raise ValueError("rate must be between 0.5 and 2.0")
        return TtsRequest(text, language, voice, rate)


@dataclass(frozen=True)
class TtsResult:
    status: str
    path: Optional[Path] = None
    cached: bool = False
    reason: Optional[str] = None
    provider: Optional[str] = None


class TtsProvider(Protocol):
    name: str

    def availability(self) -> tuple[bool, Optional[str]]: ...
    def synthesize(self, request: TtsRequest, should_abort: AbortCheck) -> bytes: ...


class DashScopeCosyVoiceProvider:
    """CosyVoice v2 adapter; credentials are read only from the environment."""

    name = "dashscope-cosyvoice-v2"

    def __init__(self, model: str = "cosyvoice-v2") -> None:
        self.model = model

    def availability(self) -> tuple[bool, Optional[str]]:
        if not os.getenv("DASHSCOPE_API_KEY", "").strip():
            try:
                from local_config import DASHSCOPE_API_KEY
                os.environ["DASHSCOPE_API_KEY"] = str(DASHSCOPE_API_KEY).strip()
            except (ImportError, AttributeError):
                pass
        if not os.getenv("DASHSCOPE_API_KEY", "").strip():
            return False, "DASHSCOPE_API_KEY is not configured"
        try:
            from dashscope.audio.tts_v2 import SpeechSynthesizer  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            return False, "dashscope SDK is not installed"
        return True, None

    def synthesize(self, request: TtsRequest, should_abort: AbortCheck) -> bytes:
        if should_abort():
            return b""
        from dashscope.audio.tts_v2 import SpeechSynthesizer
        synthesizer = SpeechSynthesizer(
            model=self.model,
            voice=request.voice,
            speech_rate=request.rate,
        )
        audio = synthesizer.call(request.text)
        if should_abort():
            return b""
        if not isinstance(audio, (bytes, bytearray)) or len(audio) <= 100:
            raise RuntimeError("TTS provider returned empty or invalid audio")
        return bytes(audio)


class WindowsSapiProvider:
    """Offline Chinese TTS fallback for the Windows lamp runtime.

    The browser shell used by the lamp is not guaranteed to expose
    ``speechSynthesis``.  Windows already ships with Chinese SAPI voices, so
    use PowerShell's System.Speech and convert the generated WAV to MP3 for
    the same browser audio path used by the cloud provider.
    """

    name = "windows-sapi"

    def __init__(self, ffmpeg: Optional[str] = None) -> None:
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or (
            r"D:\ffmpeg\bin\ffmpeg.exe" if Path(r"D:\ffmpeg\bin\ffmpeg.exe").is_file() else None
        )
        self.powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")

    def availability(self) -> tuple[bool, Optional[str]]:
        if os.name != "nt":
            return False, "Windows SAPI is only available on Windows"
        if not self.powershell:
            return False, "PowerShell is not available"
        if not self.ffmpeg:
            return False, "ffmpeg is not available"
        return True, None

    def synthesize(self, request: TtsRequest, should_abort: AbortCheck) -> bytes:
        if should_abort():
            return b""
        if not self.powershell or not self.ffmpeg:
            raise RuntimeError("Windows SAPI dependencies are unavailable")

        with tempfile.TemporaryDirectory(prefix="tts-sapi-") as directory:
            wav_path = Path(directory) / "speech.wav"
            mp3_path = Path(directory) / "speech.mp3"
            text_b64 = base64.b64encode(request.text.encode("utf-8")).decode("ascii")
            path_b64 = base64.b64encode(str(wav_path).encode("utf-8")).decode("ascii")
            script = r"""
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__TEXT__'))
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH__'))
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $synth.GetInstalledVoices() |
  Where-Object { $_.VoiceInfo.Culture.Name -eq 'zh-CN' } |
  Select-Object -First 1
if ($voice) { $synth.SelectVoice($voice.VoiceInfo.Name) }
$synth.Rate = __RATE__
$synth.Volume = 100
$synth.SetOutputToWaveFile($path)
$synth.Speak($text)
$synth.Dispose()
""".replace("__TEXT__", text_b64).replace("__PATH__", path_b64).replace(
                "__RATE__", str(max(-10, min(10, round((request.rate - 1) * 10))))
            )
            subprocess.run(
                [self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                check=True,
                capture_output=True,
                timeout=20,
            )
            if should_abort():
                return b""
            if not wav_path.is_file() or wav_path.stat().st_size <= 100:
                raise RuntimeError("Windows SAPI returned empty audio")
            subprocess.run(
                [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path), "-codec:a", "libmp3lame", "-b:a", "96k", str(mp3_path)],
                check=True,
                capture_output=True,
                timeout=20,
            )
            audio = mp3_path.read_bytes()
            if len(audio) <= 100:
                raise RuntimeError("MP3 conversion returned empty audio")
            return audio


class TtsService:
    """Safe MP3 cache with one in-flight provider call at a time."""

    def __init__(self, cache_dir: Path, provider: TtsProvider,
                 fallback_provider: Optional[TtsProvider] = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.provider = provider
        self.fallback_provider = fallback_provider
        self._provider_slot = threading.BoundedSemaphore(1)

    def cache_key(self, request: TtsRequest,
                  provider: Optional[TtsProvider] = None) -> str:
        request = request.normalized()
        provider = provider or self.provider
        payload = json.dumps({
            "cacheVersion": 2,
            "provider": provider.name,
            "model": getattr(provider, "model", None),
            "text": request.text,
            "language": request.language,
            "voice": request.voice,
            "rate": request.rate,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _cache_path(self, request: TtsRequest, provider: TtsProvider) -> Path:
        return self.cache_dir / f"{self.cache_key(request, provider)}.mp3"

    @staticmethod
    def _ready(path: Path, provider: TtsProvider, cached: bool) -> TtsResult:
        return TtsResult("ready", path=path, cached=cached, provider=provider.name)

    def _provider_for(self, preferred_provider: Optional[str]) -> TtsProvider:
        if preferred_provider and self.fallback_provider:
            if preferred_provider == self.fallback_provider.name:
                return self.fallback_provider
        return self.provider

    def synthesize(self, request: TtsRequest,
                   should_abort: Optional[AbortCheck] = None,
                   preferred_provider: Optional[str] = None) -> TtsResult:
        should_abort = should_abort or (lambda: False)
        try:
            request = request.normalized()
        except (TypeError, ValueError) as exc:
            return TtsResult("error", reason=str(exc))

        selected_provider = self._provider_for(preferred_provider)
        output = self._cache_path(request, selected_provider)
        if output.is_file() and output.stat().st_size > 100:
            return self._ready(output, selected_provider, True)
        if should_abort():
            return TtsResult("cancelled", reason="request cancelled")
        available, reason = selected_provider.availability()
        if not available and selected_provider is self.provider and self.fallback_provider:
            fallback_available, fallback_reason = self.fallback_provider.availability()
            if fallback_available:
                selected_provider = self.fallback_provider
                output = self._cache_path(request, selected_provider)
                if output.is_file() and output.stat().st_size > 100:
                    return self._ready(output, selected_provider, True)
            else:
                return TtsResult("unavailable", reason=f"{reason}; fallback unavailable: {fallback_reason}")
        elif not available:
            return TtsResult("unavailable", reason=reason)

        while not self._provider_slot.acquire(timeout=0.1):
            if should_abort():
                return TtsResult("cancelled", reason="request cancelled")
        try:
            if should_abort():
                return TtsResult("cancelled", reason="request cancelled")
            output = self._cache_path(request, selected_provider)
            if output.is_file() and output.stat().st_size > 100:
                return self._ready(output, selected_provider, True)
            try:
                audio = selected_provider.synthesize(request, should_abort)
            except Exception as primary_exc:
                if selected_provider is not self.provider or not self.fallback_provider:
                    raise
                fallback_available, fallback_reason = self.fallback_provider.availability()
                if not fallback_available:
                    raise RuntimeError(f"{primary_exc}; fallback unavailable: {fallback_reason}") from primary_exc
                selected_provider = self.fallback_provider
                output = self._cache_path(request, selected_provider)
                if output.is_file() and output.stat().st_size > 100:
                    return self._ready(output, selected_provider, True)
                try:
                    audio = selected_provider.synthesize(request, should_abort)
                except Exception as fallback_exc:
                    raise RuntimeError(
                        f"cloud provider failed: {primary_exc}; "
                        f"fallback provider failed: {fallback_exc}"
                    ) from fallback_exc
            if should_abort() or not audio:
                return TtsResult("cancelled", reason="request cancelled")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix="tts-", suffix=".tmp", dir=self.cache_dir)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(audio)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, output)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return self._ready(output, selected_provider, False)
        except Exception as exc:
            return TtsResult("error", reason=f"provider error: {exc}")
        finally:
            self._provider_slot.release()
