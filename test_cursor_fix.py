# -*- coding: utf-8 -*-
"""离屏验证：红针位置映射 + 播放自动跟随滚动。"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, r"B:\wkbuddy\时间轴对齐工具\subsync_gui")

import numpy as np
from PySide6.QtWidgets import QApplication

app = QApplication([])
from ui.waveform_widget import WaveformWidget

w = WaveformWidget()
w.resize(1200, 300)
w.set_media_duration(7200.0)
rng = np.random.default_rng(7)
mins = (-rng.integers(100, 32000, 100_000)).astype(np.int16)
maxs = rng.integers(100, 32000, 100_000).astype(np.int16)
w.set_peaks(mins, maxs, 20)
w.show()

# --- 测试 1：缩放到 600~1200s 后，播放位置 900s 的红针应在窗口中部 ---
w._set_vis(600.0, 1200.0)
w.set_play_position(900.0)
img = w.grab().toImage()
red_cols = set()
for x in range(img.width()):
    for y in (12, 80, 140, 200):
        c = img.pixelColor(x, y)
        if c.red() > 200 and c.green() < 120 and c.blue() < 120:
            red_cols.add(x)
            break
expected = round(w._time_to_px(900.0))
print(f"expected x = {expected}, red cols = {sorted(red_cols)}")
assert any(abs(x - expected) <= 3 for x in red_cols), \
    f"红针位置错误: 期望 {expected}, 实际 {sorted(red_cols)}"
print("TEST1 OK: 缩放后红针按可视窗口正确映射")

# --- 测试 2：播放位置越过视野右缘 -> 视图自动前滚 ---
w._set_vis(600.0, 1200.0)
w.set_play_position(1180.0)   # 超过 95% 缘（1140s）
assert w._vis_start_s > 600.0, f"自动跟随未触发: vis=({w._vis_start_s},{w._vis_end_s})"
print(f"TEST2 OK: 自动前滚 vis=({w._vis_start_s:.1f}, {w._vis_end_s:.1f})")

# --- 测试 3：快退到视野之前 -> 视图回滚到光标处 ---
w.set_play_position(60.0)
assert w._vis_start_s <= 60.0 + 1e-6, f"回退跟随失败: vis=({w._vis_start_s},{w._vis_end_s})"
print(f"TEST3 OK: 回退跟随 vis=({w._vis_start_s:.1f}, {w._vis_end_s:.1f})")

# --- 测试 4：全片视图不滚动（fit_all 后播放不改变视野） ---
w.fit_all()
w.set_play_position(3000.0)
assert abs(w._vis_start_s) < 1e-6, "全片视图不应滚动"
print("TEST4 OK: 全片视图保持不动")

print("ALL OK")
