# -*- coding: utf-8 -*-
"""配置管理：快捷键表 + 波形高度等 UI 偏好。

配置持久化到 exe 同目录（便携模式）或项目根目录（源码运行）的
subsync_settings.json。单文件 exe 可把配置文件一起拷走。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence


def config_path() -> Path:
    """配置文件位置：exe 同目录（frozen）或项目根目录（源码）。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        # subsync_gui/core/config.py -> 项目根
        base = Path(__file__).resolve().parents[2]
    return base / "subsync_settings.json"


# ------------------------------------------------------------------ 快捷键定义
# 每一项: id -> (显示名, 默认按键序列, 是否文本编辑时也生效)
# 按键序列格式使用 QKeySequence 字符串（如 "Ctrl+Shift+Left"、"Space"、"F5"）。
DEFAULT_SHORTCUTS: dict[str, tuple[str, str, bool]] = {
    "toggle_play":       ("播放 / 暂停", "Space", False),
    "seek_back_1s":      ("快退 1 秒", "Left", False),
    "seek_fwd_1s":       ("快进 1 秒", "Right", False),
    "seek_back_5s":      ("快退 5 秒", "Shift+Left", False),
    "seek_fwd_5s":       ("快进 5 秒", "Shift+Right", False),
    "seek_back_10s":     ("快退 10 秒", "Ctrl+Left", False),
    "seek_fwd_10s":      ("快进 10 秒", "Ctrl+Right", False),
    "jump_start":        ("跳到视频开头", "Home", False),
    "jump_end":          ("跳到视频结尾", "End", False),
    "play_current":      ("播放当前字幕", "G", False),
    "prev_subtitle":     ("上一条字幕", "Alt+Up", False),
    "next_subtitle":     ("下一条字幕", "Alt+Down", False),
    "nudge_back_100":    ("当前字幕 -0.1s", "Alt+Left", False),
    "nudge_fwd_100":     ("当前字幕 +0.1s", "Alt+Right", False),
    "nudge_back_500":    ("当前字幕 -0.5s", "Ctrl+Alt+Left", False),
    "nudge_fwd_500":     ("当前字幕 +0.5s", "Ctrl+Alt+Right", False),
    "shift_all_back":    ("全部字幕平移 -0.5s", "Ctrl+Shift+Left", False),
    "shift_all_fwd":     ("全部字幕平移 +0.5s", "Ctrl+Shift+Right", False),
    "undo":              ("撤销", "Ctrl+Z", False),
    "redo":              ("重做", "Ctrl+Y", False),
    "insert_subtitle":   ("插入字幕", "Ctrl+N", False),
    "delete_subtitle":   ("删除字幕", "Ctrl+Delete", False),
    "open_video":        ("打开视频", "Ctrl+O", True),
    "open_srt":          ("打开字幕", "Ctrl+Shift+O", True),
    "export_srt":        ("导出字幕", "Ctrl+E", True),
    "start_align":       ("一键自动对齐", "F5", True),
    "fit_view":          ("波形视图适配", "0", False),
    "show_help":         ("快捷键帮助", "F1", True),
    "show_settings":     ("快捷键设置", "F2", True),
}

WAVE_HEIGHT_DEFAULT = 120
WAVE_HEIGHT_MIN = 60
WAVE_HEIGHT_MAX = 360

# 支持拖拽/打开的文件扩展名
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".flv", ".wmv",
    ".webm", ".rmvb", ".rm", ".ogm", ".3gp", ".mpeg", ".mpg", ".vob",
}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa"}
DRAG_EXTS = VIDEO_EXTS | SUBTITLE_EXTS


def default_shortcut_table() -> dict[str, tuple[str, str, bool]]:
    return {k: v for k, v in DEFAULT_SHORTCUTS.items()}


def load_config() -> dict:
    """读取配置文件，返回 {"shortcuts": {...}, "wave_height": int}。"""
    cfg = {
        "shortcuts": {k: v[1] for k, v in DEFAULT_SHORTCUTS.items()},
        "wave_height": WAVE_HEIGHT_DEFAULT,
    }
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data.get("shortcuts"), dict):
            for k in list(cfg["shortcuts"]):
                v = data["shortcuts"].get(k)
                if isinstance(v, str) and v.strip():
                    cfg["shortcuts"][k] = v.strip()
        if isinstance(data.get("wave_height"), (int, float)):
            h = int(data["wave_height"])
            cfg["wave_height"] = max(WAVE_HEIGHT_MIN, min(WAVE_HEIGHT_MAX, h))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # 规范化按键写法（如 Ctrl+Delete -> Ctrl+Del），保证存储与显示一致
    for k in list(cfg["shortcuts"]):
        parsed = parse_key_sequence(cfg["shortcuts"][k])
        if parsed is None:
            cfg["shortcuts"][k] = DEFAULT_SHORTCUTS[k][1]
            continue
        key, mods = parsed
        norm = key_combination_to_str(key, mods)
        if norm:
            cfg["shortcuts"][k] = norm
    return cfg


def save_config(shortcuts: dict[str, str], wave_height: int) -> bool:
    """保存配置；失败返回 False（不打扰用户）。"""
    try:
        payload = {
            "shortcuts": shortcuts,
            "wave_height": int(wave_height),
        }
        config_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ 按键序列工具
_MOD_NAME = {
    Qt.ControlModifier: "Ctrl",
    Qt.AltModifier: "Alt",
    Qt.ShiftModifier: "Shift",
    Qt.MetaModifier: "Meta",
}


def mods_to_str(mods: Qt.KeyboardModifier) -> str:
    """把修饰键组合转成 QKeySequence 字符串前缀，如 Ctrl+Alt+。"""
    parts = []
    for mod, name in _MOD_NAME.items():
        if mods & mod:
            parts.append(name)
    return "+".join(parts)


def mods_to_int(mods) -> int:
    """KeyboardModifier flag -> int（兼容不同 PySide6 版本）。"""
    if hasattr(mods, "value"):
        return int(mods.value)
    return int(mods)


def key_combination_to_str(key: Qt.Key, mods: Qt.KeyboardModifier) -> str:
    """Qt 键 + 修饰键 -> "Ctrl+Shift+Left" 形式的字符串。"""
    prefix = mods_to_str(mods)
    key_name = QKeySequence(key).toString()
    if not key_name:
        key_name = QKeySequence(int(key)).toString()
    if not key_name:
        return ""
    return f"{prefix}+{key_name}" if prefix else key_name


def parse_key_sequence(seq_str: str) -> tuple[Qt.Key, Qt.KeyboardModifier] | None:
    """把 "Ctrl+Shift+Left" 解析为 (key, mods)；非法返回 None。"""
    seq = QKeySequence(seq_str)
    if seq.isEmpty():
        return None
    combo = seq[0]
    if hasattr(combo, "key") and hasattr(combo, "keyboardModifiers"):
        key = combo.key()
        mods = combo.keyboardModifiers()
    else:
        # 兼容返回 int 的情况（key 值低位）
        raw = int(combo)
        mods = Qt.KeyboardModifier(raw & int(
            Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier | Qt.MetaModifier))
        key = Qt.Key(raw & 0x01FFFFFF)
    # 去掉修饰键本身（如单独按 Ctrl 不算合法快捷键）
    if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
        return None
    return key, mods
