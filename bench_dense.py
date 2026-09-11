# -*- coding: utf-8 -*-
"""字幕密度对 alass 耗时的影响：120 分钟、3 倍对白密度"""
import sys
sys.path.insert(0, r"B:\wkbuddy\时间轴对齐工具")
import numpy as np
import bench_alass as B


def gen_dense(minutes: float):
    rng = np.random.default_rng(7)
    dur = int(minutes * 60 * B.SR)
    audio = (rng.standard_normal(dur) * 0.003).astype(np.float32)
    subs = []
    t = 0.5
    while t < minutes * 60 - 4:
        burst = rng.uniform(0.8, 2.0)      # 更短的对白
        gap = rng.uniform(0.2, 0.9)        # 更密的节奏（连续对话）
        a, b = int(t * B.SR), int((t + burst) * B.SR)
        env = rng.uniform(0.3, 1.0, b - a).astype(np.float32)
        env = np.convolve(env, np.ones(3200) / 3200, mode="same")
        audio[a:b] += rng.standard_normal(b - a).astype(np.float32) * 0.45 * env
        subs.append((t, t + burst))
        t += burst + gap
    audio = np.clip(audio, -1, 1)
    pcm = (audio * 32767).astype(np.int16)
    wav = B.WORK / f"audio_dense_{int(minutes)}min.wav"
    import wave
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(B.SR)
        w.writeframes(pcm.tobytes())
    srt = B.WORK / f"subs_dense_{int(minutes)}min.srt"
    with open(srt, "w", encoding="utf-8") as f:
        for n, (a, b) in enumerate(subs, 1):
            f.write(f"{n}\n{B.make_srt_time(a + B.SHIFT_S)} --> {B.make_srt_time(b + B.SHIFT_S)}\n密对白 {n}\n\n")
    return wav, srt, len(subs)


wav, srt, n = gen_dense(120)
print(f"[dense 120min] 字幕行={n}")
r = B.run_alass(wav, srt, "dense120")
print(r)
wav.unlink(missing_ok=True); srt.unlink(missing_ok=True)
(B.WORK / "out_dense120.srt").unlink(missing_ok=True)
