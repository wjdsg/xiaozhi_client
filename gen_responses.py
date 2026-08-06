# Author: mjw
# Date: 2026-07-27
"""Batch synthesize intent response MP3s using DashScope CosyVoice v2"""
import time, os
import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer

model = "cosyvoice-v2"
voice = "longxiaochun_v2"

items = [
    ("resp_timer_set",    "\u597d\u7684\uff0c\u5df2\u8bbe\u7f6e\u95f9\u949f"),
    ("resp_timer_cancel", "\u95f9\u949f\u5df2\u53d6\u6d88"),
    ("resp_timer_none",   "\u5f53\u524d\u6ca1\u6709\u95f9\u949f"),
    ("resp_light_on",     "\u706f\u5df2\u6253\u5f00"),
    ("resp_light_off",    "\u706f\u5df2\u5173\u95ed"),
    ("resp_brightness",   "\u4eae\u5ea6\u5df2\u8c03\u6574"),

    ("resp_brightness_low", "灯光已调到低档"),
    ("resp_brightness_mid", "灯光已调到中档"),
    ("resp_brightness_high", "灯光已调到高档"),
    ("resp_volume_0",     "\u97f3\u91cf\u5df2\u9759\u97f3"),
    ("resp_volume_25",    "\u97f3\u91cf\u5df2\u8c03\u5230\u767e\u5206\u4e4b\u4e8c\u5341\u4e94"),
    ("resp_volume_50",    "\u97f3\u91cf\u5df2\u8c03\u5230\u767e\u5206\u4e4b\u4e94\u5341"),
    ("resp_volume_75",    "\u97f3\u91cf\u5df2\u8c03\u5230\u767e\u5206\u4e4b\u4e03\u5341\u4e94"),
    ("resp_volume_100",   "\u97f3\u91cf\u5df2\u8c03\u5230\u767e\u5206\u4e4b\u4e00\u767e"),
    ("resp_dialog_exit",  "\u597d\u7684\uff0c\u5df2\u7ed3\u675f\u672c\u6b21\u5bf9\u8bdd"),
]

for filename, text in items:
    out = f"{filename}.mp3"
    if os.path.exists(out) and os.path.getsize(out) > 100:
        print(f"[{filename}] already exists, skip")
        continue

    print(f"[{filename}] synthesizing ({len(text)} chars) -> {out}")
    for attempt in range(3):
        try:
            s = SpeechSynthesizer(model=model, voice=voice)
            audio = s.call(text)
            if audio and len(audio) > 100:
                with open(out, "wb") as f:
                    f.write(audio)
                print(f"  OK {out} ({len(audio)} bytes)")
                break
            else:
                print(f"  retry {attempt+1}: got {type(audio)} len={len(audio) if audio else 0}")
        except Exception as e:
            print(f"  retry {attempt+1}: {e}")
        time.sleep(2)
    else:
        print(f"  FAILED: {filename}")

print("\n--- Results ---")
for filename, _ in items:
    out = f"{filename}.mp3"
    ok = os.path.exists(out)
    size = os.path.getsize(out) if ok else 0
    print(f"  {out}: {'OK' if ok else 'MISSING'} ({size} bytes)")
