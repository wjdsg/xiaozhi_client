# Author: mjw
# Date: 2026-07-16
"""SpeexDSP 回声消除 ctypes 封装 封装 libspeexdsp.dll 的 AEC API，提供 Pythonic 接口."""
import ctypes
import os
import threading
from ctypes import POINTER, c_int, c_void_p

import numpy as np

from src.utils.logging_config import get_logger

logger = get_logger(__name__)

SPEEX_ECHO_SET_SAMPLING_RATE = 24
SPEEX_ECHO_GET_FRAME_SIZE = 3


def _load_dll():
    candidate_paths = [
        os.path.join(os.path.dirname(__file__), "..", "..", "libs", "libspeexdsp.dll"),
        os.path.join(os.path.dirname(__file__), "libspeexdsp.dll"),
    ]
    for p in candidate_paths:
        p = os.path.abspath(p)
        if os.path.exists(p):
            return ctypes.CDLL(p)
    raise FileNotFoundError(f"libspeexdsp.dll not found in: {candidate_paths}")


class SpeexAEC:
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_size: int = 320,
        filter_length_ms: int = 200,
        frame_delay: int = 3,
    ):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.filter_length = int(sample_rate * filter_length_ms / 1000)
        self.frame_delay = frame_delay
        self._lock = threading.Lock()
        self._reference_buffer = None
        self._ref_write_idx = 0
        self._ref_ready = False
        self._prefill_count = 0
        self._state = None
        self._dll = _load_dll()

        self._setup_api()
        self._create_state()

    def _setup_api(self):
        d = self._dll
        d.speex_echo_state_init.argtypes = [c_int, c_int]
        d.speex_echo_state_init.restype = c_void_p
        d.speex_echo_state_destroy.argtypes = [c_void_p]
        d.speex_echo_state_destroy.restype = None
        d.speex_echo_cancellation.argtypes = [
            c_void_p,
            POINTER(ctypes.c_int16),
            POINTER(ctypes.c_int16),
            POINTER(ctypes.c_int16),
        ]
        d.speex_echo_cancellation.restype = None
        d.speex_echo_playback.argtypes = [c_void_p, POINTER(ctypes.c_int16)]
        d.speex_echo_playback.restype = None
        d.speex_echo_capture.argtypes = [
            c_void_p,
            POINTER(ctypes.c_int16),
            POINTER(ctypes.c_int16),
        ]
        d.speex_echo_capture.restype = None
        d.speex_echo_ctl.argtypes = [c_void_p, c_int, c_void_p]
        d.speex_echo_ctl.restype = c_int

    def _create_state(self):
        self._state = self._dll.speex_echo_state_init(
            self.frame_size, self.filter_length
        )
        if not self._state:
            raise RuntimeError("speex_echo_state_init 失败")
        sr = c_int(self.sample_rate)
        ret = self._dll.speex_echo_ctl(self._state, SPEEX_ECHO_SET_SAMPLING_RATE, ctypes.byref(sr))
        if ret != 0:
            self.destroy()
            raise RuntimeError(f"设置采样率失败: {ret}")

        buffer_frames = self.frame_delay + 10
        buffer_size = buffer_frames * self.frame_size
        self._reference_buffer = np.zeros(buffer_size, dtype=np.int16)
        self._ref_write_idx = 0
        self._prefill_count = 0

        logger.info(
            f"SpeexAEC 初始化: {self.sample_rate}Hz 帧长={self.frame_size} "
            f"滤波={self.filter_length_samples}样点 延迟={self.frame_delay}帧"
        )

    @property
    def filter_length_samples(self):
        return self.filter_length

    def destroy(self):
        if self._state:
            self._dll.speex_echo_state_destroy(self._state)
            self._state = None
        self._reference_buffer = None
        self._ref_ready = False

    def feed_reference(self, ref_16khz: np.ndarray):
        with self._lock:
            if self._reference_buffer is None or len(ref_16khz) == 0:
                return
            samples = ref_16khz.astype(np.int16)
            n = len(samples)
            buf = self._reference_buffer
            buf_size = len(buf)
            idx = self._ref_write_idx % buf_size
            remaining = buf_size - idx
            if n <= remaining:
                buf[idx:idx + n] = samples
            else:
                buf[idx:] = samples[:remaining]
                buf[:n - remaining] = samples[remaining:]
            self._ref_write_idx += n
            self._prefill_count += n
            if self._prefill_count >= self.frame_delay * self.frame_size:
                self._ref_ready = True

    def process_frame(self, mic_16khz: np.ndarray) -> np.ndarray:
        assert len(mic_16khz) == self.frame_size
        with self._lock:
            if not self._ref_ready or self._reference_buffer is None:
                return mic_16khz

            buf_size = len(self._reference_buffer)
            read_start = self._ref_write_idx - (
                self.frame_delay * self.frame_size + self.frame_size
            )
            indices = np.arange(read_start, read_start + self.frame_size) % buf_size
            ref_frame = self._reference_buffer[indices]

        mic = mic_16khz.astype(np.int16)
        out = np.zeros(self.frame_size, dtype=np.int16)
        self._dll.speex_echo_cancellation(
            self._state,
            mic.ctypes.data_as(POINTER(ctypes.c_int16)),
            ref_frame.ctypes.data_as(POINTER(ctypes.c_int16)),
            out.ctypes.data_as(POINTER(ctypes.c_int16)),
        )
        return out

    def reset(self):
        with self._lock:
            if self._state:
                self._dll.speex_echo_state_destroy(self._state)
            self._create_state()
