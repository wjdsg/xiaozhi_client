# Author: mjw
# Date: 2026-07-16
"""端侧轻量级能量检测器 用于快速判断用户是否开始讲话，触发打断事件."""
import numpy as np

from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class EnergyDetector:
    def __init__(
        self,
        threshold_rms: float = 0.008,
        hold_frames: int = 12,
        cooldown_ms: int = 800,
        sample_rate: int = 16000,
        frame_size: int = 320,
    ):
        self.threshold_rms = threshold_rms
        self.hold_frames = hold_frames
        self.cooldown_frames = int(cooldown_ms / (frame_size * 1000 / sample_rate))
        self.frame_size = frame_size

        self._active_count = 0
        self._cooldown_count = 0
        self._enabled = True
        self._on_interrupt = None

        logger.info(
            f"能量检测器: 阈值={threshold_rms:.4f} 持续={hold_frames}帧 "
            f"冷却={cooldown_ms}ms"
        )

    def set_interrupt_callback(self, callback):
        self._on_interrupt = callback

    def on_audio_data(self, audio_data: np.ndarray) -> None:
        if not self._enabled or self._on_interrupt is None:
            return

        if self._cooldown_count > 0:
            self._cooldown_count -= 1
            return

        if len(audio_data) == 0:
            return

        rms = float(np.sqrt(np.mean(audio_data.astype(np.float64) ** 2)) / 32768.0)

        if rms > self.threshold_rms:
            self._active_count += 1
        else:
            self._active_count = max(0, self._active_count - 1)

        if self._active_count >= self.hold_frames:
            self._active_count = 0
            self._cooldown_count = self.cooldown_frames
            logger.warning(f"检测到语音能量触发 (RMS={rms:.4f})")
            try:
                self._on_interrupt()
            except Exception as e:
                logger.warning(f"打断回调异常: {e}")
        elif rms > self.threshold_rms * 0.5 and self._active_count > 0:

            logger.debug(
                f"能量累积: {self._active_count}/{self.hold_frames} (RMS={rms:.4f})"
            )

    def enable(self):
        self._enabled = True
        self._active_count = 0

    def disable(self):
        self._enabled = False
        self._active_count = 0

    def reset(self):
        self._active_count = 0
        self._cooldown_count = 0
