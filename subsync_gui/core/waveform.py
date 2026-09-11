# -*- coding: utf-8 -*-
"""波形数据计算：把 PCM 压缩为峰值对（min/max）数组。

数据规模：8kHz mono，每 5ms 一块 → 每秒 200 个峰值对。
2 小时影片约 144 万个峰值对，内存与绘制均无压力。
"""
from __future__ import annotations

import numpy as np

DEFAULT_BLOCK_MS = 5
DEFAULT_SAMPLE_RATE = 8000   # 波形用 8kHz 足够，提取更快、数据更小


def compute_peaks(
    pcm_bytes: bytes,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    block_ms: int = DEFAULT_BLOCK_MS,
) -> tuple[np.ndarray, np.ndarray]:
    """从 s16le PCM 字节计算峰值对。

    返回 (mins, maxes)，两个等长 float32 数组，范围 [-32768, 32767]。
    """
    raw = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    block = max(1, int(sample_rate * block_ms / 1000.0))
    n_blocks = (len(raw) + block - 1) // block
    if n_blocks == 0:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)

    # 尾部补零凑整
    pad = n_blocks * block - len(raw)
    if pad:
        raw = np.pad(raw, (0, pad))
    mat = raw.reshape(n_blocks, block)
    mins = mat.min(axis=1)
    maxes = mat.max(axis=1)
    return mins.astype(np.float32), maxes.astype(np.float32)


def compute_peaks_stream(
    proc,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    block_ms: int = DEFAULT_BLOCK_MS,
    progress_cb=None,
    chunk_bytes: int = 1 << 20,
) -> tuple[np.ndarray, np.ndarray]:
    """从 ffmpeg 子进程的 stdout 管道**边读边算**峰值对。

    不保存完整 PCM，提取与计算并行，显著降低等待时间与内存占用。
    proc 需要有可读的 stdout（subprocess.Popen stdout=PIPE，二进制）。
    progress_cb(bytes_read) 每读一块回调一次，用于进度显示。

    返回 (mins, maxes) float32 数组。
    """
    block = max(1, int(sample_rate * block_ms / 1000.0))
    block_bytes = block * 2  # s16le
    mins_l, maxs_l = [], []
    pending = b""

    while True:
        chunk = proc.stdout.read(chunk_bytes)
        if not chunk:
            break
        if progress_cb is not None:
            progress_cb(len(chunk))
        buf = pending + chunk
        n_full = len(buf) // block_bytes
        if n_full > 0:
            data = np.frombuffer(buf[: n_full * block_bytes], dtype=np.int16)
            mat = data.reshape(n_full, block).astype(np.float32)
            mins_l.append(mat.min(axis=1))
            maxs_l.append(mat.max(axis=1))
        pending = buf[n_full * block_bytes:]

    # 尾部不足一块的采样也并入最后一个峰值块
    if pending:
        tail = np.frombuffer(pending, dtype=np.int16).astype(np.float32)
        mins_l.append(tail.min(axis=0).reshape(1))
        maxs_l.append(tail.max(axis=0).reshape(1))

    if not mins_l:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    return (
        np.concatenate(mins_l).astype(np.float32),
        np.concatenate(maxs_l).astype(np.float32),
    )


def peak_time_for_index(idx: int, block_ms: int = DEFAULT_BLOCK_MS) -> float:
    """峰值索引 → 秒。"""
    return idx * block_ms / 1000.0
