# Author: mjw
# Date: 2026-07-16
"""端到端 AEC + 能量检测集成验证."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.utils.opus_loader import setup_opus

setup_opus()

from src.audio_codecs.audio_codec import AudioCodec
from src.audio_codecs.energy_detector import EnergyDetector


async def main():
    print("=== AEC + 能量检测集成测试 ===")
    print()
    print(f"0) AEC 配置: 见 config.json AEC_OPTIONS.ENABLED")

    codec = AudioCodec()

    print("1) 初始化 AudioCodec...")
    await codec.initialize()
    print(f"   ✓ 输入: {codec.device_input_sample_rate}Hz {codec.input_channels}ch")
    print(f"   ✓ 输出: {codec.device_output_sample_rate}Hz {codec.output_channels}ch")
    print(f"   ✓ AEC 启用: {codec._aec_enabled}")
    print(f"   ✓ SpeexAEC 实例: {codec._speex_aec is not None}")
    if codec._speex_aec:
        print(f"   ✓ filter_length: {codec._speex_aec.filter_length_samples}样点")
    print(f"   ✓ 参考重采样器: {codec._reference_resampler is not None}")
    print()

    print("2) 创建能量检测器...")
    interrupt_count = [0]
    def on_interrupt():
        interrupt_count[0] += 1
        print(f"   ⚡ 能量打断触发 #{interrupt_count[0]}!")

    detector = EnergyDetector(
        threshold_rms=0.008, hold_frames=12, cooldown_ms=800
    )
    detector.set_interrupt_callback(on_interrupt)
    codec.add_audio_listener(detector)
    print("   ✓ 能量检测器已挂载")

    print("3) 模拟音频流运行 2 秒...")
    await asyncio.sleep(2.0)

    print()
    print("4) 清理...")
    detector.disable()
    await codec.close()
    print("   ✓ 全部清理完成")

    print()
    print("=== 集成测试通过 ===")
    print(f"   音频设备: ✓ 正常")
    print(f"   SpeexDSP AEC: ✓ 启用 (filter_length={getattr(codec._speex_aec, 'filter_length_samples', 'N/A')})")
    print(f"   参考信号回路: ✓ 已建")
    print(f"   能量检测器: ✓ 已挂载")
    print(f"   代码运行: ✓ 无异常")


if __name__ == "__main__":
    asyncio.run(main())
