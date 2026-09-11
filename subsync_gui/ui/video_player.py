# -*- coding: utf-8 -*-
"""视频播放器控件（基于 QtMultimedia + QGraphicsVideoItem），带字幕浮层。

QVideoWidget 在 Windows 上是原生窗口，会盖住同级的 QLabel 字幕浮层。
这里改用 QGraphicsVideoItem 把视频和字幕渲染到同一个 QGraphicsScene，
从根本上避免覆盖问题。
"""
from __future__ import annotations

from PySide6.QtCore import Signal, QUrl, Qt, QTimer, QEvent, QSizeF, QPointF, QRectF
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QSlider, QHBoxLayout, QPushButton, QStyle,
    QStyleOptionSlider, QGraphicsView, QGraphicsScene, QGraphicsTextItem,
)
from PySide6.QtGui import QFont, QColor, QDragEnterEvent, QDropEvent

from core.config import DRAG_EXTS


class ClickSlider(QSlider):
    """支持点击 track 任意位置直接跳转的 QSlider。

    默认 QSlider 在某些样式/嵌入式场景下点击 track 不响应（只能拖 thumb），
    这里重写 mousePressEvent 计算点击位置对应的 value 并跳转。
    区分点击和拖动：点击会发出 `click_jumped(int)` 信号；
    拖动走原生 sliderMoved 行为。
    """
    click_jumped = Signal(int)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            # PySide6 6.11 缺失 SE_SliderTrack 枚举，直接用 opt.rect 计算 track 范围
            if self.orientation() == Qt.Horizontal:
                pos = ev.position().x() - opt.rect.x()
                span = max(1, opt.rect.width())
            else:
                pos = ev.position().y() - opt.rect.y()
                span = max(1, opt.rect.height())
            v = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(),
                int(pos), int(span), opt.upsideDown)
            self.setValue(v)
            # 触发与拖动 thumb 相同的跳转逻辑
            self.sliderMoved.emit(v)
            # 标记：本次是"点击"而非"拖动起始"，用于上层决定是否自动播放
            self.click_jumped.emit(v)
        super().mousePressEvent(ev)


class VideoPlayer(QWidget):
    position_changed_ms = Signal(int)
    duration_changed_ms = Signal(int)
    state_changed = Signal(int)
    # 拖入文件：把视频/字幕文件路径交给主窗口处理（支持一次拖多个）
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.audio_out.setVolume(0.8)
        self.player.setAudioOutput(self.audio_out)
        self._pending_audio_track: int | None = None   # 媒体就绪后再设置

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # 视频画面容器（QGraphicsView + QGraphicsVideoItem + 字幕）
        self._video_container = QWidget(self)
        self._video_container.setMinimumSize(320, 200)
        video_lay = QVBoxLayout(self._video_container)
        video_lay.setContentsMargins(0, 0, 0, 0)
        video_lay.setSpacing(0)

        self.view = QGraphicsView(self._video_container)
        self.view.setStyleSheet("border: none; background: black;")
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        video_lay.addWidget(self.view, 1)

        self.scene = QGraphicsScene(self.view)
        self.view.setScene(self.scene)

        # 视频项
        self.video_item = QGraphicsVideoItem()
        self.scene.addItem(self.video_item)
        self.player.setVideoOutput(self.video_item)

        # 字幕浮层：白字黑底阴影，底部居中
        self.subtitle_item = QGraphicsTextItem()
        self.subtitle_item.setDefaultTextColor(QColor("#ffffff"))
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        self.subtitle_item.setFont(font)
        # 设置阴影效果（通过 HTML 样式实现描边感）
        self.scene.addItem(self.subtitle_item)
        self.subtitle_item.hide()

        # 播放失败/格式不支持提示
        self._error_item = QGraphicsTextItem()
        self._error_item.setDefaultTextColor(QColor("#cccccc"))
        err_font = QFont()
        err_font.setPointSize(12)
        self._error_item.setFont(err_font)
        self.scene.addItem(self._error_item)
        self._error_item.hide()

        lay.addWidget(self._video_container, 1)

        # 控制条
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_play = QPushButton()
        self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.btn_play.setToolTip("播放/暂停")
        self.btn_play.setFixedSize(30, 26)
        self.btn_play.clicked.connect(self.toggle_play)

        self.slider = ClickSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self._seek)
        # 点击进度条任意位置：跳转后自动开始播放
        self.slider.click_jumped.connect(lambda _v: self.play())

        self.lbl_time = QLabel("00:00:00 / 00:00:00")
        self.lbl_time.setStyleSheet("font-size: 11px; color: #555;")

        bar.addWidget(self.btn_play)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.lbl_time)
        lay.addLayout(bar)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.errorOccurred.connect(self._on_error)
        self.player.playbackStateChanged.connect(self._on_state_changed)
        self.video_item.nativeSizeChanged.connect(self._on_native_size_changed)
        # Qt 媒体就绪后，若之前请求过音轨切换则补设
        self.player.mediaStatusChanged.connect(self._on_media_status)

        self._overlay_text = ""
        self._has_error = False
        self._video_native_size = QSizeF(0, 0)

        # 确保浮层跟随窗口大小
        self._video_container.installEventFilter(self)
        self._position_overlay()

        # 支持把视频/字幕文件直接拖到画面（黑屏区域）上
        self.setAcceptDrops(True)
        self.view.setAcceptDrops(False)

    # ------------------------------------------------------------- 拖拽
    def _drag_file_paths(self, event) -> list[str]:
        mime = event.mimeData()
        if not (mime and mime.hasUrls()):
            return []
        out = []
        for url in mime.urls():
            if url.isLocalFile():
                from pathlib import Path
                p = url.toLocalFile()
                if Path(p).suffix.lower() in DRAG_EXTS:
                    out.append(p)
        return out

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._drag_file_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        paths = self._drag_file_paths(event)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    # ------------------------------------------------------------- 播放
    def load(self, path: str):
        self._has_error = False
        self._error_item.hide()
        self.player.setSource(QUrl.fromLocalFile(path))
        self.btn_play.setEnabled(True)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.pause()
        else:
            self.play()

    def play(self):
        if not self._has_error:
            self.player.play()

    def pause(self):
        self.player.pause()

    def stop(self):
        self.player.stop()

    def seek_ms(self, ms: int):
        self.player.setPosition(max(0, ms))

    def _seek(self, ms: int):
        self.player.setPosition(ms)

    def current_ms(self) -> int:
        return self.player.position()

    # ------------------------------------------------------------- 字幕浮层
    def set_subtitle_text(self, text: str):
        """更新画面上显示的字幕；空字符串则隐藏。"""
        self._overlay_text = text.strip()
        if self._overlay_text:
            # 用 HTML 实现黑底圆角 + 文字阴影，确保在复杂画面上可读
            html = (
                '<div style="'
                'display:inline-block;'
                'color:#ffffff;'
                'background-color:rgba(0,0,0,180);'
                'padding:10px 22px;'
                'border-radius:8px;'
                'text-align:center;'
                'line-height:1.5;'
                'font-size:20px;'
                'font-weight:bold;'
                'text-shadow: 2px 2px 4px #000000;'
                '">'
                f"{self._overlay_text.replace(chr(10), '<br>')}"
                '</div>'
            )
            self.subtitle_item.setHtml(html)
            self.subtitle_item.show()
        else:
            self.subtitle_item.hide()
        self._position_overlay()

    def _position_overlay(self):
        if not self._video_container.isVisible():
            return
        w = self._video_container.width()
        h = self._video_container.height()

        # 视图占满容器
        self.view.setGeometry(0, 0, w, h)
        self.scene.setSceneRect(0, 0, w, h)

        # 视频等比缩放居中
        if self._video_native_size.width() > 0 and self._video_native_size.height() > 0:
            native = self._video_native_size
            scale = min(w / native.width(), h / native.height())
            vw = native.width() * scale
            vh = native.height() * scale
            vx = (w - vw) / 2.0
            vy = (h - vh) / 2.0
            self.video_item.setSize(QSizeF(vw, vh))
            self.video_item.setPos(QPointF(vx, vy))
        else:
            self.video_item.setSize(QSizeF(w, h))
            self.video_item.setPos(QPointF(0, 0))

        # 字幕底部居中，宽度占 92%
        sw = max(200, int(w * 0.92))
        self.subtitle_item.setTextWidth(sw)
        br = self.subtitle_item.boundingRect()
        sx = (w - sw) / 2.0
        sy = h - br.height() - 16
        self.subtitle_item.setPos(QPointF(sx, sy))

        # 错误提示居中
        err_br = self._error_item.boundingRect()
        self._error_item.setPos((w - err_br.width()) / 2.0, (h - err_br.height()) / 2.0)

    def eventFilter(self, obj, ev):
        if obj is self._video_container and ev.type() == QEvent.Type.Resize:
            self._position_overlay()
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------- 音轨
    def set_active_audio_track(self, audio_track_index: int | None):
        """切换播放音轨（Qt 的音频轨序号，从 0 计；None 表示默认）。

        媒体未就绪时记录请求，等 mediaStatusChanged 触发后补设。
        """
        if audio_track_index is None:
            self._pending_audio_track = None
            return
        try:
            if self.player.mediaStatus() == QMediaPlayer.NoMedia:
                self._pending_audio_track = audio_track_index
                return
            self.player.setActiveAudioTrack(audio_track_index)
            self._pending_audio_track = None
        except Exception:
            # 某些平台/格式不支持切音轨，静默忽略
            pass

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia and self._pending_audio_track is not None:
            try:
                self.player.setActiveAudioTrack(self._pending_audio_track)
            except Exception:
                pass
            self._pending_audio_track = None

    # ------------------------------------------------------------- 回调
    def _on_position(self, ms: int):
        self.slider.blockSignals(True)
        self.slider.setValue(ms)
        self.slider.blockSignals(False)
        self.lbl_time.setText(f"{self._fmt(ms)} / {self._fmt(self.player.duration())}")
        self.position_changed_ms.emit(ms)

    def _on_duration(self, ms: int):
        self.slider.setRange(0, max(0, ms))
        self.lbl_time.setText(f"{self._fmt(self.player.position())} / {self._fmt(ms)}")
        self.duration_changed_ms.emit(ms)

    def _on_error(self, error, msg):
        self._has_error = True
        txt = f"视频预览不可用\n{msg or str(error)}\n\n音频波形与自动对齐仍可使用。"
        self._error_item.setPlainText(txt)
        self._error_item.show()
        self._position_overlay()
        self.btn_play.setEnabled(False)

    def _on_state_changed(self, state):
        self.state_changed.emit(state)
        if state == QMediaPlayer.PlayingState:
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        else:
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def _on_native_size_changed(self, size: QSizeF):
        self._video_native_size = size
        self._position_overlay()

    @staticmethod
    def _fmt(ms: int) -> str:
        ms = max(0, int(ms))
        h, rem = divmod(ms, 3_600_000)
        m, s = divmod(rem // 1000, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def has_source(self) -> bool:
        return self.player.source().isValid()
