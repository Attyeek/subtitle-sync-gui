# -*- coding: utf-8 -*-
"""alass 对齐耗时基准测试：不同时长 -> 对齐墙钟时间 / CPU 时间 / 峰值内存"""
import ctypes
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(r"B:\wkbuddy\时间轴对齐工具")
ALASS = ROOT / "build" / "tools_stage" / "alass" / "cli" / "alass-cli.exe"
WORK = ROOT / "bench_tmp"
WORK.mkdir(exist_ok=True)

SR = 16000          # 采样率（alass 内部会再降采样，这里只是生成口径）
SHIFT_S = 2.5       # 字幕相对语音的偏移量（秒）


def make_srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def gen_case(minutes: float):
    """生成类语音音频（噪声突发=对白）+ 偏移 SHIFT_S 的字幕，返回 (wav, srt)"""
    rng = np.random.default_rng(42)
    dur = int(minutes * 60 * SR)
    audio = (rng.standard_normal(dur) * 0.003).astype(np.float32)  # 底噪
    subs = []
    t = 1.0
    i = 0
    while t < minutes * 60 - 5:
        burst = rng.uniform(1.2, 3.2)              # 对白时长
        gap = rng.uniform(0.8, 2.8)                # 对白间隔
        a, b = int(t * SR), int((t + burst) * SR)
        env = rng.uniform(0.3, 1.0, b - a).astype(np.float32)
        # 平滑包络
        env = np.convolve(env, np.ones(3200) / 3200, mode="same")
        audio[a:b] += rng.standard_normal(b - a).astype(np.float32) * 0.45 * env
        subs.append((t, t + burst))
        t += burst + gap
        i += 1
    audio = np.clip(audio, -1, 1)
    pcm = (audio * 32767).astype(np.int16)

    wav = WORK / f"audio_{int(minutes)}min.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())

    srt = WORK / f"subs_{int(minutes)}min.srt"
    with open(srt, "w", encoding="utf-8") as f:
        for n, (a, b) in enumerate(subs, 1):
            f.write(f"{n}\n{make_srt_time(a + SHIFT_S)} --> {make_srt_time(b + SHIFT_S)}\n"
                    f"对白测试行 {n}\n\n")
    return wav, srt, len(subs)


# ---------- Windows 进程计时/内存 ----------
k32 = ctypes.windll.kernel32


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


def ft_to_sec(ft: FILETIME) -> float:
    return ((ft.hi << 32) | ft.lo) / 1e7


class MEMORY_BASIC_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


RUN_TIMEOUT = 1500.0   # 单次 alass 运行超时（秒）


def run_alass(wav: Path, srt: Path, tag: str):
    out = WORK / f"out_{tag}.srt"
    env = dict(os.environ)
    env["PATH"] = str(ROOT / "build" / "tools_stage" / "ffmpeg" / "bin") + os.pathsep + env["PATH"]
    cmd = [str(ALASS), str(wav), str(srt), str(out)]
    # 输出重定向到文件（alass 输出量大，PIPE 不排水会死锁——实测教训）
    log_f = open(WORK / f"alass_{tag}.log", "wb")
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, env=env)
    import psutil
    try:
        ps = psutil.Process(proc.pid)
    except Exception:
        ps = None
    peak_ws = 0
    timed_out = False
    cpu = -1.0
    while proc.poll() is None:
        try:
            if ps is not None:
                peak_ws = max(peak_ws, ps.memory_info().rss)
                ct = ps.cpu_times()
                cpu = ct.user + ct.system
        except Exception:
            pass
        if time.perf_counter() - t0 > RUN_TIMEOUT:
            timed_out = True
            proc.terminate()
            break
        time.sleep(0.15)
    wall = time.perf_counter() - t0
    log_f.close()
    ok = (not timed_out) and proc.returncode == 0 and out.is_file()
    return {"wall_s": round(wall, 2), "cpu_s": round(cpu, 2),
            "peak_mem_mb": round(peak_ws / 1e6, 1), "ok": ok, "timeout": timed_out}


def main():
    durations = [float(x) for x in sys.argv[1:]] or [5, 15, 30, 60, 120]
    results = []
    for m in durations:
        wav, srt, n_lines = gen_case(m)
        size_mb = wav.stat().st_size / 1e6
        r = run_alass(wav, srt, f"{int(m)}min")
        r.update({"minutes": m, "srt_lines": n_lines, "wav_mb": round(size_mb, 1)})
        results.append(r)
        print(f"[{m:>5.0f} min] 字幕行={n_lines:>5}  墙钟={r['wall_s']:>8.2f}s  "
              f"CPU={r['cpu_s']:>8.2f}s  峰值内存={r['peak_mem_mb']:>7.1f}MB  ok={r['ok']}",
              flush=True)
        wav.unlink(missing_ok=True)
        srt.unlink(missing_ok=True)
        (WORK / f"out_{int(m)}min.srt").unlink(missing_ok=True)
    with open(ROOT / "bench_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("saved -> bench_results.json")


if __name__ == "__main__":
    main()
