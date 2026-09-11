# -*- coding: utf-8 -*-
"""波形图 + 字幕时间轴复合控件。

布局（从上到下）：
  1. 字幕轨道：每行字幕一个色块，可点击/双击/拖拽（边缘改时长，中间整体平移）
  2. 波形区域：音频峰值（min/max）绘制，红绿配色跟随中文行情惯例无意义，这里用
     经典「靛蓝波形 + 橙色选中」配色
  3. 时间刻度条
  4. 播放光标：红色竖线贯穿全部区域

交互：
  - 鼠标滚轮：以指针位置为中心缩放时间轴
  - Shift+滚轮 / 中键拖动：平移
  - 单击空白区域：跳转播放位置
  - 单击字幕块：选中；双击：跳转播放该句
  - 拖动字幕块：调整字幕时间
"""
from __future__ import annotations

import bisect
import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QLineF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics
from PySide6.QtWidgets import QWidget, QApplication, QScrollBar, QLabel, QHBoxLayout, QSlider

from core.srt_parser import SubtitleLine, ms_to_timecode

SUB_TOP = 6
SUB_H = 34
WAVE_TOP = SUB_TOP + SUB_H + 10
WAVE_H = 120        # 默认波形高度（可用 set_wave_height 调整）
SCALE_H = 22
PAD = 6  # 左右留白

# 配色
C_BG = QColor("#f7f8fa")
C_WAVE = QColor("#4a6cf7")
C_WAVE_HOT = QColor("#2f4fd0")
C_SUB_BORDER = QColor("#8aa0d8")
C_SUB_FILL = QColor("#dbe4ff")
C_SUB_SEL_FILL = QColor("#ffb84d")
C_SUB_SEL_BORDER = QColor("#d97a00")
C_SUB_PLAY_FILL = QColor("#7ee08a")
C_SUB_PLAY_BORDER = QColor("#2e8b45")
C_CURSOR = QColor("#e5484d")
C_GRID = QColor("#e3e6ec")
C_TEXT = QColor("#33363d")
C_SCALE_LINE = QColor("#9aa3b2")


class WaveformWidget(QWidget):
    # index, new_start_ms, new_end_ms（拖动中实时发）
    subtitle_drag = Signal(int, int, int)
    subtitle_drag_finished = Signal(int, int, int)
    subtitle_drag_started = Signal(int)   # 用户开始拖动字幕块（用于撤销栈保存旧状态）
    subtitle_clicked = Signal(int)
    subtitle_double_clicked = Signal(int)
    # 用户单击时间轴空白处 -> 秒
    cursor_moved = Signal(float)
    # 波形高度改变（debounce 500ms 拖动停止后发一次）
    wave_height_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        # 波形绘制高度（可调），默认 120，范围 60~360
        self._wave_h = WAVE_H
        # 为底部滚动条预留 18px
        self.setMinimumHeight(SUB_TOP + SUB_H + self._wave_h + SCALE_H + 20 + 18)

        self._mins: np.ndarray | None = None
        self._maxs: np.ndarray | None = None
        self._peak_rate = 200.0          # 峰值块/秒（5ms）
        self._media_duration_s = 0.0

        self._subtitles: list[SubtitleLine] = []
        self._selected = -1

        self._vis_start_s = 0.0
        self._vis_end_s = 0.0
        self._play_pos_s = 0.0
        self._has_media = False

        # 拖动状态
        self._drag = None        # ("move"|"left"|"right", index, orig_start, orig_end, press_px)
        self._hover_sub = -1
        self._hover_edge = None  # "left"/"right"/None

        self._auto_gain = 1.0

        # 底部水平滚动条，用于平移时间轴
        self._scrollbar = QScrollBar(Qt.Horizontal, self)
        self._scrollbar.valueChanged.connect(self._on_scroll)
        self._scrollbar.setSingleStep(1)
        self._scrollbar.hide()
        self._sb_updating = False

        # 右上角内嵌的「波形高度」调节面板（始终可见）
        self._build_wave_height_panel()

    # ---------------------------------------------------------------- 数据
    def set_wave_height(self, height: int):
        """设置波形区域绘制高度（60~360px），即时重绘。"""
        h = max(60, min(360, int(height)))
        if h != self._wave_h:
            self._wave_h = h
            if hasattr(self, "_wave_h_slider") and self._wave_h_slider.value() != h:
                self._wave_h_slider.blockSignals(True)
                self._wave_h_slider.setValue(h)
                self._wave_h_slider.blockSignals(False)
            if hasattr(self, "_wave_h_label"):
                self._wave_h_label.setText(f"高度 {h}px")
            self.update()
            # 触发防抖保存信号（拖动停止后才发，避免频繁写盘）
            self._wave_h_save_timer.start()

    def wave_height(self) -> int:
        return self._wave_h

    def has_peaks(self) -> bool:
        """是否已生成波形数据。"""
        return self._mins is not None and len(self._mins) > 0

    def _build_wave_height_panel(self):
        """在波形图右上角内嵌一个紧凑的「高度」调节面板（始终可见、紧贴波形）。"""
        # 防抖：拖动滑块停止 500ms 后才发出 wave_height_changed
        self._wave_h_save_timer = QTimer(self)
        self._wave_h_save_timer.setSingleShot(True)
        self._wave_h_save_timer.setInterval(500)
        self._wave_h_save_timer.timeout.connect(
            lambda: self.wave_height_changed.emit(self._wave_h))

        self._wave_h_panel = QWidget(self)
        self._wave_h_panel.setObjectName("waveHPanel")
        self._wave_h_panel.setStyleSheet(
            "QWidget#waveHPanel{background:rgba(255,255,255,210);"
            "border:1px solid #c4cbd8;border-radius:5px;}"
            "QLabel{color:#33363d;font-size:11px;padding:0 4px;}"
            "QSlider::groove:horizontal{height:4px;background:#dde1e8;border-radius:2px;}"
            "QSlider::handle:horizontal{width:12px;height:12px;"
            "margin:-5px 0;background:#2f6fed;border-radius:6px;}"
            "QSlider::sub-page:horizontal{background:#2f6fed;border-radius:2px;}"
        )
        lay = QHBoxLayout(self._wave_h_panel)
        lay.setContentsMargins(4, 0, 6, 0)
        lay.setSpacing(4)
        self._wave_h_label = QLabel(f"高度 {self._wave_h}px")
        self._wave_h_slider = QSlider(Qt.Horizontal)
        self._wave_h_slider.setRange(60, 360)
        self._wave_h_slider.setValue(self._wave_h)
        self._wave_h_slider.setFixedWidth(110)
        self._wave_h_slider.setPageStep(20)
        self._wave_h_slider.setToolTip("拖动调节波形图上下高度（60~360px）")
        self._wave_h_slider.valueChanged.connect(self.set_wave_height)
        lay.addWidget(self._wave_h_label)
        lay.addWidget(self._wave_h_slider)
        self._wave_h_panel.adjustSize()
        self._position_wave_height_panel()

    def _position_wave_height_panel(self):
        if not hasattr(self, "_wave_h_panel"):
            return
        w = self._wave_h_panel.sizeHint().width()
        h = self._wave_h_panel.sizeHint().height()
        x = max(2, self.width() - w - 6)
        y = 2   # 紧贴顶部
        self._wave_h_panel.setGeometry(x, y, w, h)
        self._wave_h_panel.raise_()
    def set_peaks(self, mins: np.ndarray, maxs: np.ndarray, block_ms: int = 5):
        self._mins = mins
        self._maxs = maxs
        self._peak_rate = 1000.0 / block_ms
        self._auto_gain = self._compute_gain()
        # 无论媒体时长是否已知都强制 fit_all：
        # 时长未知时用峰值长度/字幕推断可视范围，保证波形一定显示
        self.fit_all()

    def set_media_duration(self, seconds: float):
        self._media_duration_s = seconds
        self._has_media = True
        self.fit_all()
        self._update_scrollbar()

    def _compute_gain(self) -> float:
        if self._mins is None or len(self._mins) < 10:
            return 1.0
        peak = max(np.abs(self._mins).max(), np.abs(self._maxs).max())
        return 1.0 if peak <= 0 else 32768.0 / peak

    def set_subtitles(self, subs: list[SubtitleLine]):
        self._subtitles = list(subs)
        self._selected = -1 if not subs else 0
        self.update()

    def select_subtitle(self, index: int):
        self._selected = index
        self.update()

    def set_play_position(self, sec: float):
        prev = self._play_pos_s
        self._play_pos_s = sec
        # Subtitle Edit 行为：播放时游标向前滚动，视图自动前滚保持红针可见
        if sec != prev and self._drag is None:
            self._follow_playback(sec)
        self.update()

    def _follow_playback(self, sec: float):
        """播放中自动平移视图，让红色游标始终留在视野内（SE 风格）。

        - 全片视图（未缩放）时不滚动；
        - 游标接近视野右缘（95%）或越出视野（快进/回退）时，
          把视图滚到让游标位于视野左侧 10% 处。
        """
        if self._media_duration_s <= 0:
            return
        dur = self._vis_end_s - self._vis_start_s
        if dur <= 0 or dur >= self._media_duration_s:
            return   # 全片视图：光标天然可见，无需滚动
        margin = dur * 0.05
        if self._vis_start_s <= sec <= self._vis_end_s - margin:
            return   # 仍在视野内
        new_start = sec - dur * 0.1
        new_start = min(max(0.0, new_start), max(0.0, self._media_duration_s - dur))
        self._set_vis(new_start, new_start + dur)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_scrollbar()
        self._position_wave_height_panel()

    def _layout_scrollbar(self):
        sb_h = 18
        self._scrollbar.setGeometry(0, self.height() - sb_h, self.width(), sb_h)

    def _update_scrollbar(self):
        """根据当前可视窗口 [_vis_start_s, _vis_end_s] 刷新滚动条状态。"""
        if not self._has_media or self._media_duration_s <= 0:
            self._scrollbar.hide()
            return
        dur = self._vis_end_s - self._vis_start_s
        total = self._media_duration_s
        if dur >= total:
            self._scrollbar.hide()
            return
        self._scrollbar.show()
        self._sb_updating = True
        max_val = 10000
        page = int(dur / total * max_val)
        page = max(1, min(page, max_val - 1))
        value = int(self._vis_start_s / (total - dur) * (max_val - page))
        self._scrollbar.setRange(0, max_val - page)
        self._scrollbar.setPageStep(page)
        self._scrollbar.setValue(value)
        self._sb_updating = False

    def _on_scroll(self, value: int):
        """用户拖动滚动条时平移视图（不触发滚动条自身刷新，避免循环）。"""
        if self._sb_updating:
            return
        if not self._has_media or self._media_duration_s <= 0:
            return
        total = self._media_duration_s
        dur = self._vis_end_s - self._vis_start_s
        max_val = self._scrollbar.maximum() + self._scrollbar.pageStep()
        if max_val <= 0:
            return
        new_start = value / max_val * (total - dur)
        self._vis_start_s = new_start
        self._vis_end_s = new_start + dur
        self.update()

    def fit_all(self):
        if self._media_duration_s > 0:
            self._vis_start_s = 0.0
            self._vis_end_s = self._media_duration_s
        elif self._subtitles:
            self._vis_end_s = max(s.end_ms for s in self._subtitles) / 1000.0 + 2
        elif self._mins is not None and len(self._mins) > 0:
            # 时长未知：按峰值数量推断可视范围（波形总长度 = 块数 / 块速率）
            self._vis_start_s = 0.0
            self._vis_end_s = len(self._mins) / max(1.0, self._peak_rate)
        else:
            self._vis_end_s = 60.0
        self.update()
        self._update_scrollbar()

    # ---------------------------------------------------------------- 映射
    def _px_w(self) -> float:
        return max(1.0, self.width() - 2 * PAD)

    def _time_to_px(self, sec: float) -> float:
        if self._vis_end_s <= self._vis_start_s:
            return PAD
        return PAD + (sec - self._vis_start_s) / (self._vis_end_s - self._vis_start_s) * self._px_w()

    def _px_to_time(self, px: float) -> float:
        if self._vis_end_s <= self._vis_start_s:
            return 0.0
        return self._vis_start_s + (px - PAD) / self._px_w() * (self._vis_end_s - self._vis_start_s)

    # ---------------------------------------------------------------- 交互
    def wheelEvent(self, ev):
        if not self._has_media and not self._subtitles:
            return
        delta = ev.angleDelta().y()
        if ev.modifiers() & Qt.ShiftModifier:
            # 平移
            step = (self._vis_end_s - self._vis_start_s) * 0.15
            shift = step if delta > 0 else -step
            self._pan(shift)
            return
        # 缩放，以鼠标位置为锚点
        zoom = 1.25 if delta > 0 else 0.8
        anchor_t = self._px_to_time(ev.position().x())
        dur = self._vis_end_s - self._vis_start_s
        new_dur = min(max(dur * (1.0 / zoom), 1.0), self._media_duration_s or 3600)
        ratio = anchor_t - self._vis_start_s
        new_start = anchor_t - ratio * (new_dur / dur)
        self._set_vis(new_start, new_start + new_dur)

    def _pan(self, delta_s: float):
        dur = self._vis_end_s - self._vis_start_s
        max_dur = self._media_duration_s or dur
        new_start = self._vis_start_s + delta_s
        if dur >= max_dur:
            return
        new_start = min(max(new_start, 0.0), max_dur - dur)
        self._set_vis(new_start, new_start + dur)

    def _set_vis(self, start: float, end: float):
        if end <= start:
            return
        self._vis_start_s = start
        self._vis_end_s = end
        self.update()
        self._update_scrollbar()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Left:
            self._pan(-(self._vis_end_s - self._vis_start_s) * 0.1)
        elif ev.key() == Qt.Key_Right:
            self._pan((self._vis_end_s - self._vis_start_s) * 0.1)
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        self.setFocus()
        if ev.button() == Qt.MiddleButton:
            self._drag = ("pan", 0, 0, 0, ev.position().x())
            return
        if ev.button() != Qt.LeftButton:
            return
        idx, edge = self._hit_subtitle(ev.position().x(), ev.position().y())
        if idx >= 0:
            self._selected = idx
            if edge:
                self._drag = ("resize", idx, self._subtitles[idx].start_ms,
                              self._subtitles[idx].end_ms, ev.position().x(), edge)
            else:
                self._drag = ("move", idx, self._subtitles[idx].start_ms,
                              self._subtitles[idx].end_ms, ev.position().x())
            self.subtitle_drag_started.emit(idx)
            self.subtitle_clicked.emit(idx)
            self.update()
        else:
            # 点击空白：跳转，并允许按住拖动连续定位（红针实时跟随鼠标）
            t = self._px_to_time(ev.position().x())
            if 0 <= t <= (self._media_duration_s or t):
                self._play_pos_s = t
                self.cursor_moved.emit(t)
                self._drag = ("seek", 0, 0, 0, ev.position().x())
                self.update()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._drag:
            kind = self._drag[0]
            if kind == "seek":
                # 按住空白区拖动：红针连续跟随鼠标，播放位置实时跳转
                t = self._px_to_time(pos.x())
                if 0 <= t <= (self._media_duration_s or t):
                    self._play_pos_s = t
                    self.cursor_moved.emit(t)
                    self.update()
                return
            if kind == "pan":
                self._pan((self._drag[4] - pos.x()) / self._px_w() * (self._vis_end_s - self._vis_start_s))
                self._drag = ("pan", 0, 0, 0, pos.x())
                return
            idx = self._drag[1]
            orig_start = self._drag[2]
            orig_end = self._drag[3]
            sub = self._subtitles[idx]
            dt = self._px_to_time(pos.x()) * 1000 - self._px_to_time(self._drag[4]) * 1000
            if kind == "move":
                ns = orig_start + dt
                ne = orig_end + dt
            elif kind == "resize" and self._drag[5] == "left":
                ns = orig_start + dt
                ne = orig_end
            else:  # right
                ns = orig_start
                ne = orig_end + dt
            ns, ne = int(round(ns)), int(round(ne))
            min_dur = 120  # 最短 0.12s
            if ne - ns < min_dur:
                if kind == "left":
                    ns = ne - min_dur
                else:
                    ne = ns + min_dur
            ns = max(0, ns)
            sub.start_ms, sub.end_ms = ns, ne
            self.subtitle_drag.emit(idx, ns, ne)
            self.update()
        else:
            idx, edge = self._hit_subtitle(pos.x(), pos.y())
            self._hover_sub = idx
            self._hover_edge = edge
            if edge:
                self.setCursor(Qt.SizeHorCursor)
            elif idx >= 0:
                self.setCursor(Qt.OpenHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, ev):
        if self._drag and self._drag[0] in ("move", "resize"):
            idx = self._drag[1]
            sub = self._subtitles[idx]
            self.subtitle_drag_finished.emit(idx, sub.start_ms, sub.end_ms)
        self._drag = None

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            idx, _ = self._hit_subtitle(ev.position().x(), ev.position().y())
            if idx >= 0:
                self._selected = idx
                self.subtitle_double_clicked.emit(idx)
                self.update()

    def _hit_subtitle(self, px: float, py: float):
        """返回 (index, edge)。edge: 'left'/'right'/None。"""
        if not (SUB_TOP - 4 <= py <= SUB_TOP + SUB_H + 4):
            return -1, None
        t = self._px_to_time(px) * 1000
        starts = [s.start_ms for s in self._subtitles]
        i0 = bisect.bisect_right(starts, int(t)) - 1
        i0 = max(0, i0)
        for i in range(i0, len(self._subtitles)):
            s = self._subtitles[i]
            if s.start_ms > t + 5000:
                break
            x0 = self._time_to_px(s.start_ms / 1000.0)
            x1 = self._time_to_px(s.end_ms / 1000.0)
            if abs(px - x0) <= 5:
                return i, "left"
            if abs(px - x1) <= 5:
                return i, "right"
            if x0 <= px <= x1:
                return i, None
        return -1, None

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), C_BG)

        w = self.width()
        # 区域分割线
        p.setPen(QPen(C_GRID, 1))
        p.drawLine(0, SUB_TOP + SUB_H + 4, w, SUB_TOP + SUB_H + 4)
        p.drawLine(0, WAVE_TOP + self._wave_h + 4, w, WAVE_TOP + self._wave_h + 4)

        self._draw_subtitles(p)
        self._draw_waveform(p)
        self._draw_scale(p)
        self._draw_cursor(p)

    def _draw_subtitles(self, p: QPainter):
        if not self._subtitles:
            return
        # 二分找到第一个可能可见的字幕（按开始时间）
        first_ms = int(self._vis_start_s * 1000)
        starts = [s.start_ms for s in self._subtitles]
        i0 = bisect.bisect_right(starts, first_ms) - 1
        i0 = max(0, i0)
        for i in range(i0, len(self._subtitles)):
            s = self._subtitles[i]
            x0 = self._time_to_px(s.start_ms / 1000.0)
            x1 = self._time_to_px(s.end_ms / 1000.0)
            if x1 < PAD - 10:
                continue
            if x0 > self.width() - PAD + 10:
                break
            playing = s.start_ms <= self._play_pos_s * 1000 <= s.end_ms
            selected = (i == self._selected)
            if selected:
                fill, border = C_SUB_SEL_FILL, C_SUB_SEL_BORDER
            elif playing:
                fill, border = C_SUB_PLAY_FILL, C_SUB_PLAY_BORDER
            else:
                fill, border = C_SUB_FILL, C_SUB_BORDER
            p.setBrush(QBrush(fill))
            p.setPen(QPen(border, 1))
            r = QRectF(x0, SUB_TOP, max(x1 - x0, 3.0), SUB_H)
            p.drawRoundedRect(r, 3, 3)
            # 文字
            if x1 - x0 > 46:
                p.setPen(C_TEXT)
                f = QFont(self.font())
                f.setPointSize(8)
                p.setFont(f)
                txt = (s.text.split("\n")[0] if s.text else f"#{i+1}")
                metrics = QFontMetrics(f)
                elided = metrics.elidedText(txt, Qt.ElideRight, int(x1 - x0 - 8))
                p.drawText(QRectF(x0 + 4, SUB_TOP, x1 - x0 - 8, SUB_H),
                           Qt.AlignVCenter | Qt.AlignLeft, elided)
            # 边缘把手
            if x1 - x0 > 14:
                p.setPen(QPen(QColor("#5a6ca0"), 1))
                p.drawLine(int(x0 + 1), int(SUB_TOP + 6), int(x0 + 1), int(SUB_TOP + SUB_H - 6))
                p.drawLine(int(x1 - 1), int(SUB_TOP + 6), int(x1 - 1), int(SUB_TOP + SUB_H - 6))

    def _draw_waveform(self, p: QPainter):
        if self._mins is None or len(self._mins) == 0:
            p.setPen(C_TEXT)
            p.drawText(QRectF(PAD, WAVE_TOP, self.width() - 2 * PAD, self._wave_h),
                       Qt.AlignCenter, "加载视频后此处显示音频波形")
            return
        x0_px = int(max(PAD, self._time_to_px(self._vis_start_s)))
        x1_px = int(min(self.width() - PAD, self._time_to_px(self._vis_end_s)))
        n_px = max(1, x1_px - x0_px)
        s_idx = int(self._vis_start_s * self._peak_rate)
        e_idx = int(self._vis_end_s * self._peak_rate)
        s_idx = max(0, s_idx)
        e_idx = min(len(self._mins), e_idx)

        mid_y = WAVE_TOP + self._wave_h / 2
        half_h = (self._wave_h - 10) / 2 * self._auto_gain
        if half_h > self._wave_h / 2:
            half_h = self._wave_h / 2

        if e_idx - s_idx <= 0:
            return

        # 全静音轨道（音轨存在但 PCM 全 0）：画一条虚线中线 + 提示文字
        if self._auto_gain <= 1.0 and np.max(np.abs(self._maxs[s_idx:e_idx])) < 1.0 \
                and np.max(np.abs(self._mins[s_idx:e_idx])) < 1.0:
            p.setPen(QPen(C_SCALE_LINE, 1, Qt.DashLine))
            p.drawLine(int(x0_px), int(mid_y), int(x1_px), int(mid_y))
            p.setPen(C_TEXT)
            p.drawText(QRectF(PAD, WAVE_TOP, self.width() - 2 * PAD, self._wave_h),
                       Qt.AlignCenter, "音频轨道存在但全为静音（无波形）")
            return

        # 聚合到每像素
        if e_idx - s_idx > n_px:
            edges = np.linspace(s_idx, e_idx, n_px + 1).astype(np.int64)
            edges = np.unique(edges)
            seg_mins = np.minimum.reduceat(self._mins[s_idx:e_idx], edges[:-1] - s_idx)
            seg_maxs = np.maximum.reduceat(self._maxs[s_idx:e_idx], edges[:-1] - s_idx)
        else:
            seg_mins = self._mins[s_idx:e_idx]
            seg_maxs = self._maxs[s_idx:e_idx]
            edges = np.arange(s_idx, e_idx + 1)

        xs = PAD + (edges[:-1] - s_idx) / (e_idx - s_idx) * (x1_px - x0_px)
        y_top = mid_y - seg_maxs.astype(np.float64) / 32768.0 * half_h
        y_bot = mid_y - seg_mins.astype(np.float64) / 32768.0 * half_h

        # 批量绘制垂直线段（QLineF 列表一次 drawLines）
        p.setPen(QPen(C_WAVE, 1))
        p.drawLines([QLineF(float(x), float(yt), float(x), float(yb))
                     for x, yt, yb in zip(xs, y_top, y_bot)])

        # 中线
        p.setPen(QPen(C_GRID, 1, Qt.DashLine))
        p.drawLine(x0_px, int(mid_y), x1_px, int(mid_y))

    def _draw_scale(self, p: QPainter):
        dur = self._vis_end_s - self._vis_start_s
        px_per_s = self._px_w() / max(dur, 1e-6)
        # 选择刻度步长，使刻度间距在 60~200px
        steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
        step = steps[-1]
        for s in steps:
            if s * px_per_s >= 60:
                step = s
                break
        y0 = WAVE_TOP + self._wave_h + 8
        p.setPen(C_SCALE_LINE)
        f = QFont(self.font())
        f.setPointSize(8)
        p.setFont(f)
        start_t = int(self._vis_start_s // step) * step
        t = start_t
        guard = 0
        show_hours = self._media_duration_s >= 3600
        while t <= self._vis_end_s and guard < 4000:
            x = self._time_to_px(t)
            if PAD - 2 <= x <= self.width() - PAD + 2:
                p.drawLine(int(x), y0, int(x), y0 + 5)
                p.setPen(C_TEXT)
                label = self._format_scale_time(int(t * 1000), show_hours)
                p.drawText(QRectF(x - 45, y0 + 6, 90, SCALE_H - 8),
                           Qt.AlignHCenter | Qt.AlignTop, label)
                p.setPen(C_SCALE_LINE)
            t += step
            guard += 1

    @staticmethod
    def _format_scale_time(ms: int, show_hours: bool) -> str:
        """返回时间刻度字符串；>1小时视频显示 HH:MM:SS，否则 MM:SS。"""
        ms = max(0, ms)
        h, r = divmod(ms, 3_600_000)
        m, r = divmod(r, 60_000)
        s = r // 1000
        if show_hours:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _draw_cursor(self, p: QPainter):
        # 播放游标按**当前可视窗口**映射（Subtitle Edit 行为）：
        # x = _time_to_px(播放位置)，缩放/平移后红针位置依然准确；
        # 播放中的视图自动跟随由 _follow_playback 负责。
        # （旧实现按全片总时长映射，一旦缩放时间轴红针就会错位）
        x = self._time_to_px(self._play_pos_s)
        if x < PAD - 10 or x > self.width() - PAD + 10:
            return
        p.setPen(QPen(C_CURSOR, 2))
        p.drawLine(int(x), SUB_TOP - 4, int(x), WAVE_TOP + self._wave_h + SCALE_H)
        # 顶部三角
        tri = [QPointF(x, SUB_TOP - 8), QPointF(x - 5, SUB_TOP - 1),
               QPointF(x + 5, SUB_TOP - 1)]
        p.setBrush(C_CURSOR)
        p.setPen(Qt.NoPen)
        p.drawPolygon(tri)
