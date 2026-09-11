# -*- coding: utf-8 -*-
"""用 ffmpeg 从视频提取音频为 16bit PCM，供波形计算使用。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

# Windows 下禁止子进程弹出 CMD 黑窗；非 Windows 平台忽略
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def list_audio_tracks(media_path: str | Path, ffprobe_path: str | Path) -> list[dict]:
    """探测媒体文件的音频轨列表。

    返回形如 [{"stream_index": 1, "language": "eng", "title": "...",
              "channels": 2, "codec": "aac"}, ...]。
    失败（无法探测/无音频轨）返回空列表。
    """
    cmd = [
        str(ffprobe_path),
        "-v", "error",
        "-select_streams", "a",
        "-show_entries",
        "stream=index,channels,codec_name:stream_tags=language,title",
        "-of", "json",
        str(media_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60.0, creationflags=_CREATE_NO_WINDOW)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except Exception:
        return []
    tracks = []
    for st in data.get("streams", []):
        tags = st.get("tags", {}) or {}
        tracks.append({
            "stream_index": st.get("index"),
            "language": tags.get("language", ""),
            "title": tags.get("title", ""),
            "channels": st.get("channels"),
            "codec": st.get("codec_name", ""),
        })
    return tracks


def extract_pcm_s16le(
    media_path: str | Path,
    ffmpeg_path: str | Path,
    sample_rate: int = 16000,
    channels: int = 1,
    track_index: int | None = None,
    timeout: float = 600.0,
) -> bytes:
    """提取音频为 s16le 小端 PCM 字节流（mono, 16kHz 默认）。

    track_index 为 ffmpeg 的 stream index（如 0:1 对应的 1），
    指定后仅提取该音轨；None 表示 ffmpeg 默认选择最佳音轨。

    返回裸 PCM 字节；失败抛出 RuntimeError（含 ffmpeg 错误输出）。
    """
    cmd = [
        str(ffmpeg_path),
        "-hide_banner", "-loglevel", "error",
        "-i", str(media_path),
    ]
    if track_index is not None:
        cmd += ["-map", f"0:{track_index}"]
    cmd += [
        "-vn",
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-f", "s16le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, creationflags=_CREATE_NO_WINDOW)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg 提取音频失败:\n{err}")
    return proc.stdout


def extract_audio_wav(
    media_path: str | Path,
    ffmpeg_path: str | Path,
    track_index: int,
    out_path: str | Path,
    timeout: float = 900.0,
) -> str:
    """把指定音轨提取为 16kHz mono WAV 文件（供 alass 作为参考音频）。

    返回输出文件路径；失败抛出 RuntimeError。
    """
    cmd = [
        str(ffmpeg_path),
        "-hide_banner", "-loglevel", "error",
        "-i", str(media_path),
        "-map", f"0:{track_index}",
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-y",
        str(out_path),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, creationflags=_CREATE_NO_WINDOW)
    if proc.returncode != 0 or not Path(out_path).is_file():
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"提取音轨 {track_index} 失败:\n{err}")
    return str(out_path)


def extract_pcm_stream(
    media_path: str | Path,
    ffmpeg_path: str | Path,
    sample_rate: int = 8000,
    channels: int = 1,
    track_index: int | None = None,
) -> subprocess.Popen:
    """启动 ffmpeg 以管道模式提取 s16le PCM，返回 Popen（stdout 为二进制管道）。

    调用方应边读 proc.stdout 边处理，结束后 proc.wait()。
    失败会在读取阶段暴露（管道提前关闭）。
    """
    cmd = [
        str(ffmpeg_path),
        "-hide_banner", "-loglevel", "error",
        "-i", str(media_path),
    ]
    if track_index is not None:
        cmd += ["-map", f"0:{track_index}"]
    cmd += [
        "-vn",
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-f", "s16le",
        "pipe:1",
    ]
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    )


def probe_duration(media_path: str | Path, ffprobe_path: str | Path) -> float:
    """用 ffprobe 查询媒体时长（秒）。失败返回 0。"""
    cmd = [
        str(ffprobe_path),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60.0, creationflags=_CREATE_NO_WINDOW)
        if proc.returncode == 0:
            return float(proc.stdout.decode().strip())
    except Exception:
        pass
    return 0.0
