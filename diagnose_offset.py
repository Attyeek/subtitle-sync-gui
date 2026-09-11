# -*- coding: utf-8 -*-
"""《僵尸新娘》这类对齐偏移问题的快速诊断脚本。

用法（在项目根目录的 PowerShell/CMD 里）：
    python diagnose_offset.py "视频路径.mp4" "原字幕路径.srt" ["已对齐字幕路径.srt"]

会输出：
- 视频帧率、时长、音频流信息（检查是否 23.976/25fps、是否有延迟）
- 原字幕/对齐字幕的首尾时间对比
- 直接跑 alass 并打印它实际给出的偏移决策
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "subsync_gui"))

from core.alass_runner import find_tools, AlassConfig
from core.srt_parser import read_srt_file, parse_srt


def ffprobe(path: str, ffprobe_path: str) -> dict:
    cmd = [
        ffprobe_path, "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=codec_type,r_frame_rate,avg_frame_rate,start_time,time_base,duration",
        "-of", "json",
        path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    if out.returncode != 0:
        return {"error": out.stderr}
    return json.loads(out.stdout)


def frac_to_float(s: str) -> float | None:
    try:
        a, b = s.split("/")
        return float(a) / float(b)
    except Exception:
        return None


def alass_run(reference: str, srt_in: str, cfg: AlassConfig, mode: str = "auto"):
    out = Path(tempfile.gettempdir()) / f"alass_diag_{os.getpid()}.srt"
    out.unlink(missing_ok=True)
    cmd = [cfg.alass_path, reference, srt_in, str(out)]
    if mode == "shift":
        cmd.append("--no-split")
    elif mode == "split":
        cmd.extend(["--split-penalty", "7"])
    env = dict(os.environ)
    if cfg.ffmpeg_path:
        env["ALASS_FFMPEG_PATH"] = cfg.ffmpeg_path
    if cfg.ffprobe_path:
        env["ALASS_FFPROBE_PATH"] = cfg.ffprobe_path
    p = subprocess.run(cmd, capture_output=True, env=env, timeout=900, creationflags=subprocess.CREATE_NO_WINDOW)
    return {
        "rc": p.returncode,
        "stdout": p.stdout.decode("utf-8", errors="replace"),
        "stderr": p.stderr.decode("utf-8", errors="replace"),
        "output": str(out) if out.is_file() else None,
    }


def summarize_srt(path: str):
    try:
        content, _ = read_srt_file(path)
        subs = parse_srt(content)
        if not subs:
            return {"count": 0, "first": None, "last": None}
        return {
            "count": len(subs),
            "first": f"{subs[0].start_ms/1000:.3f}s - {subs[0].text[:40].replace(chr(10), ' ')}",
            "last": f"{subs[-1].start_ms/1000:.3f}s - {subs[-1].text[:40].replace(chr(10), ' ')}",
        }
    except Exception as e:
        return {"error": str(e)}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    video = sys.argv[1]
    srt = sys.argv[2]
    aligned = sys.argv[3] if len(sys.argv) > 3 else None

    cfg = find_tools()
    print("=" * 60)
    print("工具路径")
    print(f"  alass : {cfg.alass_path or '未找到'}")
    print(f"  ffmpeg: {cfg.ffmpeg_path or '未找到'}")
    print(f"  ffprobe:{cfg.ffprobe_path or '未找到'}")
    if not cfg.alass_path:
        print("错误：未找到 alass-cli.exe")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("视频信息")
    info = ffprobe(video, cfg.ffprobe_path or "ffprobe")
    if "error" in info:
        print(f"  ffprobe 失败: {info['error']}")
    else:
        fmt = info.get("format", {})
        print(f"  容器时长: {float(fmt.get('duration', 0)):.3f}s")
        for i, st in enumerate(info.get("streams", [])):
            if st.get("codec_type") == "video":
                r = frac_to_float(st.get("r_frame_rate", "0/1"))
                a = frac_to_float(st.get("avg_frame_rate", "0/1"))
                print(f"  视频流 #{i}: r_frame_rate={st.get('r_frame_rate')} ({r:.3f} fps), avg={st.get('avg_frame_rate')} ({a:.3f} fps)")
            elif st.get("codec_type") == "audio":
                print(f"  音频流 #{i}: start_time={st.get('start_time')}, time_base={st.get('time_base')}, duration={st.get('duration')}")

    print("\n" + "=" * 60)
    print("字幕信息")
    print(f"  原字幕: {summarize_srt(srt)}")
    if aligned:
        print(f"  对齐后: {summarize_srt(aligned)}")

    print("\n" + "=" * 60)
    print("用 alass 默认模式（自动）重新跑一遍…")
    res = alass_run(video, srt, cfg, mode="auto")
    print(f"  returncode: {res['rc']}")
    print("  --- 偏移决策（shifted block ... by ...） ---")
    shifts = re.findall(r"shifted block of .*? by\s+([+-]?\d:\d{2}:\d{2}\.\d{3})", res["stdout"], re.I)
    for s in shifts[-10:]:
        print(f"    {s}")
    if not shifts:
        print("    （未匹配到偏移信息，alass 可能整体失败或未输出）")
    print("\n  --- 可能的错误/警告（stderr 末尾） ---")
    print(res["stderr"][-500:] or "(empty)")
    print("\n  --- stdout 末尾（用于确认阶段） ---")
    print(res["stdout"][-500:] or "(empty)")

    if res["output"]:
        print("\n" + "=" * 60)
        print("本次诊断生成的临时对齐结果")
        print(f"  {res['output']}")


if __name__ == "__main__":
    main()
