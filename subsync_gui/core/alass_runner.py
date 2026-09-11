# -*- coding: utf-8 -*-
"""封装 alass-cli.exe 的调用。

自动探测本机已安装的 alass（用户已下载 alass.batch-bat 包）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 常见安装位置（按优先级）
_CANDIDATE_DIRS = [
    r"F:\alass.batch-bat",
    r"F:\alass",
    r"C:\alass",
    r"D:\alass",
]


@dataclass
class AlassConfig:
    alass_path: str = ""
    ffmpeg_path: str = ""
    ffprobe_path: str = ""


def _exe_in_dir(d: str, name: str) -> str:
    p = Path(d) / name
    return str(p) if p.is_file() else ""


def _meipass() -> str:
    """PyInstaller 单文件模式下的解包临时目录；源码运行时返回空。"""
    return getattr(sys, "_MEIPASS", "") or ""


def find_tools() -> AlassConfig:
    """探测 alass-cli.exe 与 ffmpeg.exe。找不到则返回空。

    优先级：随 exe 捆绑的解包目录（PyInstaller） > 环境变量 > PATH > 常见目录。
    """
    cfg = AlassConfig()
    # 0) 随 exe 捆绑的工具（PyInstaller 解包目录，全自包含）
    mp = _meipass()
    if mp:
        bundled_alass = Path(mp) / "alass" / "cli" / "alass-cli.exe"
        bundled_ff = Path(mp) / "alass" / "ffmpeg" / "bin"
        if bundled_alass.is_file():
            cfg.alass_path = str(bundled_alass)
            if (bundled_ff / "ffmpeg.exe").is_file():
                cfg.ffmpeg_path = str(bundled_ff / "ffmpeg.exe")
            if (bundled_ff / "ffprobe.exe").is_file():
                cfg.ffprobe_path = str(bundled_ff / "ffprobe.exe")
            return cfg
    # 1) 环境变量
    for var in ("ALASS_BIN", "ALASS_PATH"):
        v = os.environ.get(var, "")
        if v and Path(v).is_file():
            cfg.alass_path = v
            break
        elif v:
            cand = Path(v) / "alass-cli.exe"
            if cand.is_file():
                cfg.alass_path = str(cand)
                break
    # 2) PATH
    if not cfg.alass_path:
        found = shutil.which("alass-cli")
        if found:
            cfg.alass_path = found
    # 3) 常见目录
    if not cfg.alass_path:
        for d in _CANDIDATE_DIRS:
            p = _exe_in_dir(d, "bin/alass-cli.exe") or _exe_in_dir(d, "alass-cli.exe")
            if p:
                cfg.alass_path = p
                break

    # ffmpeg：优先 alass 同包内的 ffmpeg，其次 PATH
    if cfg.alass_path:
        base = Path(cfg.alass_path).parent
        for rel in ("ffmpeg/bin/ffmpeg.exe", "../ffmpeg/bin/ffmpeg.exe",
                    "ffmpeg.exe", "../ffmpeg/ffmpeg.exe"):
            p = (base / rel).resolve()
            if p.is_file():
                cfg.ffmpeg_path = str(p)
                break
        for rel in ("ffmpeg/bin/ffprobe.exe", "../ffmpeg/bin/ffprobe.exe",
                    "ffprobe.exe", "../ffmpeg/ffprobe.exe"):
            p = (base / rel).resolve()
            if p.is_file():
                cfg.ffprobe_path = str(p)
                break
    if not cfg.ffmpeg_path:
        cfg.ffmpeg_path = shutil.which("ffmpeg") or ""
    if not cfg.ffprobe_path:
        cfg.ffprobe_path = shutil.which("ffprobe") or ""
    return cfg


@dataclass
class AlassResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    output_path: str = ""


def run_alass(
    reference: str,          # 视频或参考字幕
    input_srt: str,
    output_srt: str,
    cfg: AlassConfig,
    mode: str = "auto",      # "shift"=仅整体平移 | "split"=智能分段 | "auto"=由调用方解析
    timeout: float = 900.0,
) -> AlassResult:
    """调用 alass 同步字幕。

    reference 可以是视频文件（从中抽音频）或另一份正确时轴的字幕。

    mode 说明（关键！）：
    - "shift": 加 --no-split，所有字幕行使用同一个偏移量。
      适合字幕整体偏移几秒、影片中间没有删减的情况。对白稀少的电影
      （如 2001 太空漫游）务必用此模式，避免分段算法在无对白段落乱切。
    - "split": 显式 --split-penalty 7（alass 默认），允许分段对齐。
      适合影片中间有删减/插入片段、前后偏移不一致的情况。
    - "auto": 不加额外参数，由调用方（GUI）先判断再传 shift 或 split。
    """
    if not cfg.alass_path:
        return AlassResult(False, -1, "", "未找到 alass-cli.exe，请检查软件路径")

    cmd = [cfg.alass_path, str(reference), str(input_srt), str(output_srt)]
    if mode == "shift":
        cmd.append("--no-split")
    elif mode == "split":
        cmd.extend(["--split-penalty", "7"])

    env = dict(os.environ)
    if cfg.ffmpeg_path:
        env["ALASS_FFMPEG_PATH"] = cfg.ffmpeg_path
    if cfg.ffprobe_path:
        env["ALASS_FFPROBE_PATH"] = cfg.ffprobe_path

    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return AlassResult(False, -1, "", f"超时（>{int(timeout)}s），任务被终止")
    except FileNotFoundError:
        return AlassResult(False, -1, "", f"无法启动 {cfg.alass_path}")

    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    ok = proc.returncode == 0 and Path(output_srt).is_file()
    return AlassResult(ok, proc.returncode, out, err, str(output_srt))
