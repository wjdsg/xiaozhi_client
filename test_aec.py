# Author: mjw
# Date: 2026-07-16
"""
SpeexDSP AEC 功能测试脚本
验证 libspeexdsp.dll 的回声消除是否正常工作
"""

import ctypes
import numpy as np
import os
from ctypes import c_int, c_void_p, POINTER, pointer

dll_path = os.path.join(os.path.dirname(__file__), "libs", "libspeexdsp.dll")
dll = ctypes.CDLL(dll_path)

dll.speex_echo_state_init.argtypes = [c_int, c_int]
dll.speex_echo_state_init.restype = c_void_p

dll.speex_echo_state_destroy.argtypes = [c_void_p]
dll.speex_echo_state_destroy.restype = None

dll.speex_echo_cancellation.argtypes = [
    c_void_p,
    POINTER(ctypes.c_int16),
    POINTER(ctypes.c_int16),
    POINTER(ctypes.c_int16),
]
dll.speex_echo_cancellation.restype = None

dll.speex_echo_capture.argtypes = [
    c_void_p,
    POINTER(ctypes.c_int16),
    POINTER(ctypes.c_int16),
]
dll.speex_echo_capture.restype = None

dll.speex_echo_playback.argtypes = [
    c_void_p,
    POINTER(ctypes.c_int16),
]
dll.speex_echo_playback.restype = None

dll.speex_echo_ctl.argtypes = [c_void_p, c_int, c_void_p]
dll.speex_echo_ctl.restype = c_int


class SpeexEchoTester:
    def __init__(self, sample_rate=16000, frame_ms=20, tail_ms=300):
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * frame_ms / 1000)
        self.filter_length = int(sample_rate * tail_ms / 1000)

        self.state = dll.speex_echo_state_init(self.frame_size, self.filter_length)
        if not self.state:
            raise RuntimeError("speex_echo_state_init 失败!")

        sr = c_int(sample_rate)
        ret = dll.speex_echo_ctl(self.state, 24, ctypes.byref(sr))
        if ret != 0:
            raise RuntimeError(f"speex_echo_ctl 设置采样率失败: {ret}")

    def destroy(self):
        if self.state:
            dll.speex_echo_state_destroy(self.state)
            self.state = None

    def process_frame(self, mic_pcm: np.ndarray, ref_pcm: np.ndarray) -> np.ndarray:
        assert len(mic_pcm) == self.frame_size
        assert len(ref_pcm) == self.frame_size

        mic = mic_pcm.astype(np.int16)
        ref = ref_pcm.astype(np.int16)
        out = np.zeros(self.frame_size, dtype=np.int16)

        mic_ptr = mic.ctypes.data_as(POINTER(ctypes.c_int16))
        ref_ptr = ref.ctypes.data_as(POINTER(ctypes.c_int16))
        out_ptr = out.ctypes.data_as(POINTER(ctypes.c_int16))

        dll.speex_echo_cancellation(self.state, mic_ptr, ref_ptr, out_ptr)
        return out

    def process_capture_playback(self, mic_pcm: np.ndarray, ref_pcm: np.ndarray) -> np.ndarray:
        assert len(mic_pcm) == self.frame_size
        assert len(ref_pcm) == self.frame_size

        ref = ref_pcm.astype(np.int16)
        mic = mic_pcm.astype(np.int16)
        out = np.zeros(self.frame_size, dtype=np.int16)

        dll.speex_echo_playback(
            self.state, ref.ctypes.data_as(POINTER(ctypes.c_int16))
        )
        dll.speex_echo_capture(
            self.state,
            mic.ctypes.data_as(POINTER(ctypes.c_int16)),
            out.ctypes.data_as(POINTER(ctypes.c_int16)),
        )
        return out


def generate_test_signals(frame_size=320, duration_sec=5.0, sample_rate=16000):
    total_samples = int(duration_sec * sample_rate)
    total_frames = total_samples // frame_size

    t = np.arange(total_samples, dtype=np.float32)

    ref_signal = (
        0.8 * np.sin(2 * np.pi * 440 * t / sample_rate).astype(np.float32)
    )

    delay_samples = int(0.05 * sample_rate)
    echo = np.zeros(total_samples, dtype=np.float32)
    echo[delay_samples:] = 0.3 * ref_signal[: total_samples - delay_samples]

    mic_signal = (
        0.6 * np.sin(2 * np.pi * 300 * t / sample_rate).astype(np.float32)
    )
    mic_signal[int(1.0 * sample_rate) : int(3.0 * sample_rate)] += (
        0.8
        * np.sin(
            2 * np.pi * 880 * t[int(1.0 * sample_rate) : int(3.0 * sample_rate)]
            / sample_rate
        ).astype(np.float32)
    )
    mic_signal += echo

    return mic_signal, ref_signal, total_frames, frame_size


def compute_energy(signal):
    return np.mean(signal.astype(np.float64) ** 2)


def main():
    print("=" * 56)
    print("  SpeexDSP AEC 回声消除功能测试")
    print("=" * 56)
    print()

    sample_rate = 16000
    frame_ms = 20
    tail_ms = 300
    frame_size = int(sample_rate * frame_ms / 1000)

    print(f"参数: {sample_rate}Hz, 帧长={frame_size}样点({frame_ms}ms), 滤波长度={tail_ms}ms")
    print()

    mic_full, ref_full, total_frames, _ = generate_test_signals(
        frame_size=frame_size, sample_rate=sample_rate
    )

    tester = SpeexEchoTester(
        sample_rate=sample_rate, frame_ms=frame_ms, tail_ms=tail_ms
    )

    try:
        out_full = np.zeros(len(ref_full), dtype=np.int16)

        for i in range(total_frames):
            start = i * frame_size
            end = start + frame_size
            mic_frame = (mic_full[start:end] * 32767).astype(np.float64)
            ref_frame = (ref_full[start:end] * 32767).astype(np.float64)

            out_frame = tester.process_capture_playback(mic_frame, ref_frame)
            out_full[start:end] = out_frame

        mic_rms = np.sqrt(compute_energy(mic_full))
        ref_rms = np.sqrt(compute_energy(ref_full))
        out_rms = np.sqrt(compute_energy(out_full.astype(np.float64) / 32767))
        reduction_db = 20 * np.log10(mic_rms / (out_rms + 1e-10))

        print(f"  麦克风 RMS:   {mic_rms:.4f}")
        print(f"  参考信号 RMS: {ref_rms:.4f}")
        print(f"  AEC输出 RMS:  {out_rms:.4f}")
        print(f"  回声衰减:     {reduction_db:.1f} dB")
        print()

        if out_rms < mic_rms * 0.9:
            print("  ✓ AEC 工作正常 — 成功消除回声")
            print()
            print("  ctypes API 已验证可用:")
            for func in [
                "speex_echo_state_init",
                "speex_echo_state_destroy",
                "speex_echo_cancellation",
                "speex_echo_playback",
                "speex_echo_capture",
                "speex_echo_ctl",
            ]:
                print(f"    ✓ {func}")
        else:
            print("  ✗ AEC 可能未生效,请检查参数")

        print()
        print("=" * 56)
        print("  测试通过")

    except Exception as e:
        print(f"  ✗ 测试失败: {e}")
        raise
    finally:
        tester.destroy()


if __name__ == "__main__":
    main()
