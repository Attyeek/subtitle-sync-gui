# -*- coding: utf-8 -*-
"""主窗口：Subtitle Edit 风格布局。

左侧：字幕列表 + 文本/时间编辑区
中间：视频画面（带字幕浮层）
底部：波形时间轴
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import time
from pathlib import Path

import psutil  # 对齐期间采样 alass 进程 CPU，用于"活着/卡死"判定

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFileDialog, QMessageBox, QToolBar, QSplitter, QProgressBar, QComboBox,
    QApplication, QTextEdit, QLineEdit, QTableWidget,
)

from core.srt_parser import parse_srt, format_srt, read_srt_file, SubtitleLine
from core.audio_extract import (
    extract_pcm_s16le, probe_duration, list_audio_tracks, extract_audio_wav,
    extract_pcm_stream,
)
from core.waveform import compute_peaks, compute_peaks_stream
from core.alass_runner import find_tools, run_alass, AlassConfig, AlassResult
from core.config import (
    DEFAULT_SHORTCUTS, load_config, save_config, parse_key_sequence,
    mods_to_int, WAVE_HEIGHT_DEFAULT, WAVE_HEIGHT_MIN, WAVE_HEIGHT_MAX,
    VIDEO_EXTS, SUBTITLE_EXTS,
)
from ui.waveform_widget import WaveformWidget
from ui.video_player import VideoPlayer
from ui.subtitle_panel import SubtitlePanel
from ui.shortcut_dialog import ShortcutDialog


# ---------------------------------------------------------------- 工作线程
class AudioLoadThread(QThread):
    progress = Signal(str)
    progress_pct = Signal(int)                       # 0~99 波形生成进度
    finished_ok = Signal(object, object, float, int)   # mins, maxs, duration_s, block_ms
    failed = Signal(str)

    SAMPLE_RATE = 8000   # 波形用 8kHz 足够，提取更快、数据更小
    _STREAM_TIMEOUT = 240.0   # 流式提取超时（秒）：超时强制终止 ffmpeg 并报错

    def __init__(self, video_path: str, cfg: AlassConfig,
                 track_index: int | None = None, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.cfg = cfg
        self.track_index = track_index

    def run(self):
        import threading
        try:
            # 先探测时长（后台完成，避免阻塞 UI）
            dur = probe_duration(self.video_path, self.cfg.ffprobe_path)
            self.progress.emit("正在提取音频并生成波形…")
            proc = extract_pcm_stream(self.video_path, self.cfg.ffmpeg_path,
                                      sample_rate=self.SAMPLE_RATE,
                                      track_index=self.track_index)
            # 长视频自适应：>1小时用 20ms 块，减少内存与绘制压力
            block_ms = 20 if dur > 3600 else 5
            total_samples = int(dur * self.SAMPLE_RATE) if dur > 0 else 0
            read_samples = [0]

            def _on_chunk(nbytes: int):
                read_samples[0] += nbytes // 2
                if total_samples > 0:
                    pct = min(99, int(read_samples[0] / total_samples * 100))
                    self.progress_pct.emit(pct)

            # 边提取边计算：不用等整段音频提取完，显著减少等待
            # 放在独立线程里执行 + 超时保护：ffmpeg 挂起/管道死锁时强制终止，
            # 避免"正在提取音频并生成波形"永久卡住
            result: dict = {}

            def _stream_worker():
                result["mins"], result["maxs"] = compute_peaks_stream(
                    proc, sample_rate=self.SAMPLE_RATE, block_ms=block_ms,
                    progress_cb=_on_chunk)

            worker = threading.Thread(target=_stream_worker, daemon=True)
            worker.start()
            worker.join(self._STREAM_TIMEOUT)
            if worker.is_alive():
                try:
                    proc.terminate()
                except Exception:
                    pass
                worker.join(3)
                raise RuntimeError(
                    f"音频提取超时（>{int(self._STREAM_TIMEOUT)}s），已强制终止。\n"
                    "可能原因：视频文件异常、被占用，或音轨损坏。")
            rc = proc.wait()
            err = proc.stderr.read().decode("utf-8", errors="replace").strip()
            if rc != 0:
                raise RuntimeError(f"ffmpeg 提取音频失败:\n{err[-500:] or '未知错误'}")
            mins, maxs = result.get("mins"), result.get("maxs")
            if mins is None or len(mins) == 0:
                raise RuntimeError("未提取到任何音频（视频可能没有音轨）")
            self.finished_ok.emit(mins, maxs, dur, block_ms)
        except Exception as e:
            self.failed.emit(str(e))


class TrackProbeThread(QThread):
    """后台探测视频音轨列表。"""
    done = Signal(list)   # [{"stream_index","language","title","channels","codec"}, ...]
    failed = Signal(str)

    def __init__(self, video_path: str, cfg: AlassConfig, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.cfg = cfg

    def run(self):
        if not self.cfg.ffprobe_path:
            self.failed.emit("未找到 ffprobe")
            return
        try:
            tracks = list_audio_tracks(self.video_path, self.cfg.ffprobe_path)
            self.done.emit(tracks)
        except Exception as e:
            self.failed.emit(str(e))


class AlignThread(QThread):
    """异步运行 alass：双管道流式读取 + 真实进度解析，支持中途取消。"""
    progress = Signal(int)              # 0~99（当前阶段的真实进度）
    stage = Signal(str)                 # 当前阶段名：提取音频/帧率估算/同步字幕
    log = Signal(str)                  # 实时行（非进度条刷屏）
    done = Signal(object)              # AlassResult
    failed = Signal(str)

    # alass 2.0.0 真实进度行格式（\r 刷新，实测在 stdout）：
    #   `12 / 75 [====>----] 16.00 % 14956.33/s 0s `
    _PROG_RE = __import__("re").compile(r"(\d+)\s*/\s*(\d+)\s*\[.*?\]\s*([\d.]+)\s*%")
    _DONE_RE = __import__("re").compile(r"finished in")
    _STAGE_PATTERNS = [
        (__import__("re").compile(r"extracting audio", __import__("re").I), "提取音频"),
        (__import__("re").compile(r"guessing framerate", __import__("re").I), "帧率估算"),
        (__import__("re").compile(r"synchronizing", __import__("re").I), "同步字幕"),
    ]
    # 长电影（2 小时）的分段对齐正常就需要 5~20 分钟，
    # 超时放宽到 30 分钟：过早强杀会把"慢"误判成"卡死"
    _TIMEOUT = 1800.0

    def __init__(self, reference, srt_in, srt_out, cfg, mode, parent=None):
        super().__init__(parent)
        self.reference = reference
        self.srt_in = srt_in
        self.srt_out = srt_out
        self.cfg = cfg
        self.mode = mode
        self._cancel = False
        self._proc = None

    def request_cancel(self):
        """主线程调用，请求中止。会 terminate 进程。"""
        self._cancel = True
        if hasattr(self, "_cancel_evt") and self._cancel_evt is not None:
            self._cancel_evt.set()
        p = self._proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    def run(self):
        import os as _os
        cmd = [self.cfg.alass_path, str(self.reference), str(self.srt_in), str(self.srt_out)]
        if self.mode == "shift":
            cmd.append("--no-split")
        elif self.mode == "split":
            cmd.extend(["--split-penalty", "7"])
        env = dict(_os.environ)
        if self.cfg.ffmpeg_path:
            env["ALASS_FFMPEG_PATH"] = self.cfg.ffmpeg_path
        if self.cfg.ffprobe_path:
            env["ALASS_FFPROBE_PATH"] = self.cfg.ffprobe_path

        try:
            self._proc = __import__("subprocess").Popen(
                cmd, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE,
                env=env,
                creationflags=__import__("subprocess").CREATE_NO_WINDOW,
            )
            self.pid = self._proc.pid   # 供主线程 CPU 活体监测采样
        except Exception as e:
            self.failed.emit(f"无法启动 alass: {e}")
            return

        total = 1
        done = 0
        # stdout / stderr 都要用独立线程流式读取：
        # 1. stdout 若不排空，管道缓冲写满后 alass 进程会永久阻塞（真·停滞）
        # 2. 主循环只做 join 超时监测，避免 readline 永久阻塞本线程
        import threading
        import time as _time
        out_lines: list[str] = []
        err_lines: list[str] = []
        # 供 request_cancel 跨线程通信
        cancel_evt = threading.Event()
        self._cancel_evt = cancel_evt
        # 阶段跟踪（实测顺序：extracting audio → Guessing framerate → synchronizing）
        cur_stage = ""

        def _parse_progress(line: str):
            """解析 alass 真实进度行：`12 / 75 [====>----] 16.00 % 速度/s 剩余s`

            stdout / stderr 都会调用（实测进度在 stdout）。
            """
            nonlocal total, done, cur_stage
            # 阶段切换
            for rx, name in self._STAGE_PATTERNS:
                if rx.search(line):
                    if name != cur_stage:
                        cur_stage = name
                        self.stage.emit(name)
                    break
            m = self._PROG_RE.search(line)
            if m:
                d, t, p = int(m.group(1)), int(m.group(2)), float(m.group(3))
                total = max(total, t)
                # 当前阶段内单调递增：((done-1) + 段内百分比) / total
                pct = int(((d - 1) + min(p, 99.99) / 100.0) / max(t, 1) * 100)
                self.progress.emit(max(0, min(99, pct)))
            elif self._DONE_RE.search(line):
                done += 1
                self.progress.emit(max(1, min(99, int(done / total * 100))))

        def _reader(pipe, sink: list):
            try:
                for raw in iter(pipe.readline, b""):
                    if cancel_evt.is_set():
                        break
                    try:
                        line = raw.decode("utf-8", errors="replace")
                    except Exception:
                        line = ""
                    if not line:
                        continue
                    # alass 用 \r 刷新同一行进度条，管道里会积攒成一段超长字符串。
                    # 取最后一个 \r 之后的有效内容，避免把整段历史一次性抛给 UI。
                    if "\r" in line:
                        line = line.rsplit("\r", 1)[-1]
                    line = line.rstrip()
                    if not line:
                        continue
                    # 过滤纯进度条刷屏行（如 "alass: 1/6[======] 16.67%"），
                    # 这些行除了让状态栏爆炸外对排错无用。
                    is_progress_spam = ("[" in line and "]" in line and "%" in line)
                    sink.append(line[-1000:])   # 只保留末尾 1000 字符用于事后排错
                    if not is_progress_spam:
                        self.log.emit(line[-500:])
                    _parse_progress(line)
            except Exception:
                pass

        r_out = threading.Thread(target=_reader, args=(self._proc.stdout, out_lines), daemon=True)
        r_err = threading.Thread(target=_reader, args=(self._proc.stderr, err_lines), daemon=True)
        r_out.start()
        r_err.start()
        # 等待 stderr EOF（进程结束后管道自然关闭）；超时说明 alass 真挂死
        r_err.join(self._TIMEOUT)
        if r_err.is_alive():
            # 超时：强制终止 alass
            try:
                self._proc.terminate()
            except Exception:
                pass
            r_err.join(3)
            r_out.join(3)
            self._proc = None
            self.failed.emit(
                f"对齐超时（>{int(self._TIMEOUT // 60)} 分钟），已强制终止 alass。\n"
                "可能原因：视频无法解码、字幕格式异常、磁盘空间不足。")
            return

        # reader 已退出（stderr EOF 读完），等 alass 进程退出
        r_out.join(10)
        try:
            rc = self._proc.wait(timeout=60)
        except Exception:
            try:
                self._proc.terminate()
                rc = self._proc.wait(timeout=3)
            except Exception:
                rc = -1
        self._proc = None

        if self._cancel or cancel_evt.is_set():
            self.failed.emit("用户已停止对齐")
            return

        from core.alass_runner import AlassResult
        from pathlib import Path
        ok = (rc == 0) and Path(self.srt_out).is_file()
        result = AlassResult(
            ok=ok, returncode=rc,
            stdout="\n".join(out_lines).strip(),
            stderr="\n".join(err_lines).strip(),
            output_path=str(self.srt_out),
        )
        if ok:
            self.progress.emit(100)
            self.done.emit(result)
        else:
            self.failed.emit(
                f"alass 失败 (rc={rc}):\n{(result.stderr or result.stdout or '未知错误')[-800:]}"
            )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("字幕时间轴对齐工具 · SubSync GUI")
        self.resize(1500, 900)
        self.setMinimumSize(1100, 720)

        self.cfg = find_tools()
        self._debug_log(
            f"启动: alass={'有' if self.cfg.alass_path else '无'} "
            f"ffmpeg={'有' if self.cfg.ffmpeg_path else '无'} "
            f"ffprobe={'有' if self.cfg.ffprobe_path else '无'}")
        self.video_path = ""
        self.srt_path = ""
        self.subs: list[SubtitleLine] = []
        self.duration_ms = 0
        self._loaded_video_path = ""   # 最近一次加载的视频（用于去重）

        self._audio_thread = None
        self._track_thread = None
        self._align_thread = None
        self._last_align_mode = "auto"
        self._undo_stack: list[list[SubtitleLine]] = []   # 每次编辑前深拷贝旧状态
        self._redo_stack: list[list[SubtitleLine]] = []   # Ctrl+Y 重做
        self._undo_max = 50
        # 音轨：当前波形/对齐正在使用的 ffmpeg stream index（None=默认）
        self._active_track: int | None = None
        # 播放音轨联动：记录 Qt 音轨序号（音频流序号，0 起），None=不联动
        self._play_track: int | None = None
        self._audio_tracks: list[dict] = []
        self._pending_track_wav: str | None = None   # 对齐用临时音轨 wav（完成后删除）

        # 配置：快捷键 + 波形高度
        self._cfg_data = load_config()
        self._shortcuts: dict[str, str] = dict(self._cfg_data["shortcuts"])
        self._shortcut_map: dict[tuple[int, int], tuple] = {}
        self._build_shortcut_map()

        self._build_ui()
        # 应用已保存的波形高度（滑块内嵌在 WaveformWidget 右上角，自动持久化）
        self.waveform.set_wave_height(self._cfg_data["wave_height"])
        self.waveform.wave_height_changed.connect(self._on_wave_height_changed)
        self._apply_style()
        self._show_tool_status()
        self.setAcceptDrops(True)  # 支持把视频/字幕文件直接拖进窗口

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        # 工具栏
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        self.addToolBar(tb)

        act_open_video = QAction("打开视频", self)
        act_open_video.setToolTip("打开视频（Ctrl+O）")
        act_open_video.triggered.connect(self.open_video)
        tb.addAction(act_open_video)

        act_open_srt = QAction("打开字幕", self)
        act_open_srt.setToolTip("打开字幕（Ctrl+Shift+O）")
        act_open_srt.triggered.connect(self.open_srt)
        tb.addAction(act_open_srt)

        act_undo = QAction("撤销", self)
        act_undo.setToolTip("撤销上一步字幕编辑（Ctrl+Z）")
        act_undo.triggered.connect(self.undo)
        tb.addAction(act_undo)

        act_redo = QAction("重做", self)
        act_redo.setToolTip("重做被撤销的编辑（Ctrl+Y）")
        act_redo.triggered.connect(self.redo)
        tb.addAction(act_redo)

        act_help = QAction("快捷键", self)
        act_help.setToolTip("查看全部快捷键（F1）")
        act_help.triggered.connect(self.show_shortcut_help)
        tb.addAction(act_help)

        tb.addSeparator()

        self.btn_align = QPushButton("▶ 一键自动对齐")
        self.btn_align.setToolTip("调用 alass 自动校正字幕时间轴")
        self.btn_align.clicked.connect(self.start_align)
        self.btn_align.setEnabled(False)
        tb.addWidget(self.btn_align)

        # 停止按钮：与"一键对齐"并列，对齐中显示，点击终止
        self.btn_cancel_align = QPushButton("⏹ 停止")
        self.btn_cancel_align.setToolTip("中止正在运行的对齐任务")
        self.btn_cancel_align.setMinimumSize(72, 28)
        self.btn_cancel_align.setStyleSheet(
            "QPushButton{background:#d9534f;color:white;border:1px solid #c9302c;"
            "border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#c9302c;}"
            "QPushButton:disabled{background:#aaa;color:#eee;}")
        self.btn_cancel_align.setVisible(False)
        self.btn_cancel_align.clicked.connect(self._cancel_align)
        tb.addWidget(self.btn_cancel_align)

        # 对齐模式选择（关键：对白少 / 只差几秒的片子用"仅整体平移"最稳）
        tb.addSeparator()
        tb.addWidget(QLabel(" 对齐模式："))
        self.align_mode = QComboBox()
        self.align_mode.addItem("自动判断（推荐）", "auto")
        self.align_mode.addItem("仅整体平移（差几秒用这个）", "shift")
        self.align_mode.addItem("智能分段（影片中间有删减）", "split")
        self.align_mode.setToolTip(
            "自动判断：比较字幕与视频总时长，差异 <10% 用整体平移，否则用分段。\n\n"
            "仅整体平移：所有字幕统一平移同一个偏移量。字幕本来只差几秒、\n"
            "或对白稀少的电影（如 2001 太空漫游）务必用此模式，最稳。\n\n"
            "智能分段：alass 按内容分段对齐，适合影片中间有删减/插入片段、\n"
            "前后偏移不一致的情况。")
        tb.addWidget(self.align_mode)

        # 音轨选择（多音轨视频：选对白音轨用于波形与自动对齐）
        tb.addSeparator()
        tb.addWidget(QLabel(" 音轨："))
        self.track_combo = QComboBox()
        self.track_combo.addItem("自动（默认音轨）", None)
        self.track_combo.setToolTip(
            "选择用于波形图与自动对齐的音轨。\n"
            "多音轨视频（如原声+评论、原声+国语）请选择包含对白的音轨，\n"
            "否则对齐会以错误的音轨为参照而失败。\n\n"
            "切换后会自动重新生成波形，并联动切换播放音轨。")
        self.track_combo.setEnabled(False)
        self.track_combo.currentIndexChanged.connect(self._on_track_changed)
        tb.addWidget(self.track_combo)

        act_fit = QAction("视图适配", self)
        act_fit.setToolTip("波形缩放到全部时长（0）")
        act_fit.triggered.connect(lambda: self.waveform.fit_all())
        tb.addAction(act_fit)

        tb.addSeparator()

        act_export = QAction("导出字幕", self)
        act_export.setToolTip("导出当前字幕为 SRT（Ctrl+E）")
        act_export.triggered.connect(self.export_srt)
        tb.addAction(act_export)

        # 中央区域
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 主分割：左 = 字幕列表面板，右 = 视频 + 波形
        main_splitter = QSplitter(Qt.Horizontal)

        self.panel = SubtitlePanel()
        self.panel.setMinimumWidth(320)
        main_splitter.addWidget(self.panel)

        right_splitter = QSplitter(Qt.Vertical)
        self.player = VideoPlayer()
        self.waveform = WaveformWidget()
        right_splitter.addWidget(self.player)
        right_splitter.addWidget(self.waveform)
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setSizes([540, 240])
        main_splitter.addWidget(right_splitter)

        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 3)
        main_splitter.setSizes([360, 1100])
        root.addWidget(main_splitter, 1)

        # 进度条（用于对齐/加载；波形加载显示百分比，对齐显示忙碌动画）
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # 状态栏
        self.statusBar().showMessage("就绪。打开视频和字幕开始工作。")

        # 联动
        self.player.position_changed_ms.connect(self._on_play_pos)
        self.player.duration_changed_ms.connect(self._on_duration)
        self.player.files_dropped.connect(self._on_player_drop)
        self.waveform.cursor_moved.connect(self._seek_to)
        self.waveform.subtitle_double_clicked.connect(self._jump_and_play)
        self.waveform.subtitle_drag.connect(self._on_wave_drag)
        self.waveform.subtitle_drag_finished.connect(self._on_wave_drag_finished)
        self.panel.row_selected.connect(self.waveform.select_subtitle)
        self.panel.row_double_clicked.connect(self._jump_and_play)
        self.panel.times_edited.connect(self._on_panel_edit)
        self.panel.text_edited.connect(self._on_panel_text_edit)
        self.panel.edit_started.connect(self._save_undo_state)
        self.waveform.subtitle_drag_started.connect(self._save_undo_state)

    def _apply_style(self):
        self.setStyleSheet("""
        QMainWindow { background: #f2f4f8; }
        QToolBar { background: #ffffff; border-bottom: 1px solid #dde1e8; spacing: 6px; padding: 4px 8px; }
        QToolBar QToolButton { padding: 4px 10px; border-radius: 5px; }
        QToolBar QToolButton:hover { background: #e8edf7; }
        QPushButton { padding: 5px 12px; border-radius: 5px; border: 1px solid #c4cbd8;
                      background: #ffffff; }
        QPushButton:hover { background: #eef2fb; }
        QPushButton:disabled { color: #a0a6b2; }
        QPushButton#primaryBtn { background: #2f6fed; color: white; border: none; font-weight: bold; }
        QPushButton#primaryBtn:hover { background: #2458c4; }
        QLabel { color: #33363d; }
        QStatusBar { background: #ffffff; border-top: 1px solid #dde1e8; }
        QTableWidget { background: #ffffff; border: 1px solid #dde1e8;
                       gridline-color: #eef0f4; alternate-background-color: #fafbfd; }
        QHeaderView::section { background: #f0f3f9; border: none; padding: 4px 6px;
                               font-weight: bold; color: #44506a; }
        QSplitter::handle { background: #dde1e8; width: 3px; }
        QProgressBar { border: 1px solid #c4cbd8; border-radius: 4px; text-align: center; height: 14px; }
        QProgressBar::chunk { background: #2f6fed; border-radius: 3px; }
        QTextEdit { background: #ffffff; border: 1px solid #dde1e8; border-radius: 4px; padding: 4px; }
        QLineEdit { background: #ffffff; border: 1px solid #dde1e8; border-radius: 4px; padding: 3px; }
        """)
        self.btn_align.setObjectName("primaryBtn")

    def _show_tool_status(self):
        if self.cfg.alass_path:
            self.statusBar().showMessage(
                f"alass 已就绪  |  ffmpeg：{Path(self.cfg.ffmpeg_path).name}")
        else:
            self.statusBar().showMessage("⚠ 未找到 alass-cli.exe，自动对齐不可用（可手动拖拽/编辑字幕）")

    # ---------------------------------------------------------------- 打开文件
    def open_video(self):
        filters = (
            "所有视频文件 (*.mp4 *.mkv *.avi *.mov *.ts *.m2ts *.flv *.wmv *.webm "
            "*.rmvb *.rm *.ogm *.3gp *.mpeg *.mpg *.vob);;"
            "MP4 (*.mp4);;MKV (*.mkv);;AVI (*.avi);;RMVB (*.rmvb);;所有文件 (*)"
        )
        path, _ = QFileDialog.getOpenFileName(self, "选择视频文件", "", filters)
        if not path:
            return
        self._load_video(path)

    def _load_video(self, path: str, auto_match_subtitle: bool = True):
        """加载视频：播放画面 + 自动匹配同名字幕 + 后台生成波形。

        auto_match_subtitle：为 False 时跳过同名字幕自动加载
        （拖入视频+字幕时，用拖入的字幕，避免被同名匹配干扰）。

        幂等：重复拖入同一个视频时，不重复提取音频，波形/字幕保留；
        波形仍在生成时拖入也不会阻塞字幕加载。
        """
        # 同一视频去重：波形已生成则跳过音频重载，避免波形被覆盖/卡死
        same_video = (self._loaded_video_path and Path(self._loaded_video_path).resolve()
                      == Path(path).resolve())
        keep_wave = same_video and self.waveform.has_peaks()
        self.video_path = path
        self.player.load(path)
        self.setWindowTitle(f"字幕时间轴对齐工具 - {os.path.basename(path)}")
        # 尝试自动匹配同名字幕
        if auto_match_subtitle and not self.subs:
            cand = Path(path).with_suffix(".srt")
            if cand.is_file():
                self._load_srt_file(str(cand))
            else:
                for sfx in ("_TV.srt", ".ass", ".ssa"):
                    c2 = Path(str(Path(path).with_suffix("")) + sfx)
                    if c2.is_file():
                        self._load_srt_file(str(c2))
                        break
        # 后台加载音频波形（同视频且已有波形时保留，不重复提取）
        busy = self._audio_thread is not None and self._audio_thread.isRunning()
        if keep_wave:
            self.statusBar().showMessage(f"视频已加载过，波形保留（{os.path.basename(path)}）")
        elif busy and not same_video:
            # 波形仍在生成中：画面已切换，字幕不受影响；波形稍后由旧线程完成
            self.statusBar().showMessage("波形仍在生成中，视频画面已切换；如需更新波形请稍后再拖入")
        elif self.cfg.ffmpeg_path:
            self._start_audio_load(path, track_index=None)
        else:
            QMessageBox.warning(self, "缺少 ffmpeg", "未找到 ffmpeg，无法生成波形图。\n可手动配置或下载 ffmpeg 后重试。")
        self._loaded_video_path = path
        # 后台探测音轨列表（填充音轨下拉框；同视频重复拖入时跳过）
        if not same_video:
            self._reset_track_combo()
            if self.cfg.ffprobe_path:
                self._start_track_probe(path)
        # 视频加载后刷新对齐按钮状态
        self.btn_align.setEnabled(bool(self.subs and self.cfg.alass_path))

    # ---------------------------------------------------------------- 拖拽识别
    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._drag_paths(event):
            self.statusBar().showMessage("松开鼠标加载文件…")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.statusBar().showMessage("就绪。打开视频和字幕开始工作。")
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        paths = self._drag_paths(event)
        if not paths:
            event.ignore()
            return
        self._handle_dropped_paths(paths)
        event.acceptProposedAction()

    def _on_player_drop(self, paths: list[str]):
        """视频画面（黑屏）区域拖入文件：同样按扩展名分发加载。"""
        if paths:
            self._handle_dropped_paths(paths)

    def _handle_dropped_paths(self, paths: list[str]):
        videos = [p for p in paths if Path(p).suffix.lower() in VIDEO_EXTS]
        subs = [p for p in paths if Path(p).suffix.lower() in SUBTITLE_EXTS]
        # 视频优先：只拖视频时允许自动匹配同名字幕；
        # 视频+字幕一起拖时，用拖入的字幕（跳过同名匹配）。
        if videos:
            self._load_video(videos[0], auto_match_subtitle=not subs)
        for s in subs:
            self._load_srt_file(s)
        if videos or subs:
            if subs:
                n = len(subs)
                self.statusBar().showMessage(
                    f"已拖入 {len(videos)} 个视频、{n} 个字幕（最后加载的字幕生效）")
            else:
                self.statusBar().showMessage(f"已拖入视频：{os.path.basename(videos[0])}")
        else:
            self.statusBar().showMessage("拖入的文件不是受支持的视频或字幕格式")

    @staticmethod
    def _drag_paths(event) -> list[str]:
        """从拖拽事件中提取本地文件路径列表（空则不接受）。"""
        mime = event.mimeData()
        if not (mime and mime.hasUrls()):
            return []
        out = []
        for url in mime.urls():
            if url.isLocalFile():
                p = url.toLocalFile()
                if Path(p).suffix.lower() in (VIDEO_EXTS | SUBTITLE_EXTS):
                    out.append(p)
        return out

    def open_srt(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择字幕文件", "", "字幕文件 (*.srt *.ass *.ssa);;所有文件 (*)")
        if path:
            self._load_srt_file(path)

    def _load_srt_file(self, path: str):
        try:
            content, encoding = read_srt_file(path)
        except Exception as e:
            QMessageBox.critical(self, "读取失败", f"无法读取字幕文件：\n{e}")
            return
        if path.lower().endswith((".ass", ".ssa")):
            subs = self._ass_to_subs(content)
            if subs is None:
                return
        else:
            subs = parse_srt(content)
        if not subs:
            QMessageBox.warning(self, "字幕为空", "未能从文件中解析出任何字幕条目。")
            return
        self.subs = subs
        self.srt_path = path
        self.waveform.set_subtitles(subs)
        self.panel.set_subtitles(subs)
        self.btn_align.setEnabled(bool(self.video_path and self.cfg.alass_path))
        self.statusBar().showMessage(
            f"已加载字幕：{os.path.basename(path)}（{len(subs)} 条，编码：{encoding}）")

    @staticmethod
    def _ass_to_subs(content: str):
        """简易 ASS/SSA 解析（仅对话行，忽略样式特效）。"""
        import re
        out = []
        for line in content.splitlines():
            line = line.strip()
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            start = MainWindow._ass_time(parts[1])
            end = MainWindow._ass_time(parts[2])
            text = parts[9].replace("\\N", "\n").replace("\\n", "\n")
            text = re.sub(r"\{[^}]*\}", "", text)
            if text.strip():
                out.append(SubtitleLine(start_ms=start, end_ms=end, text=text.strip()))
        if not out:
            return None
        for i, s in enumerate(out, 1):
            s.index = i
        return out

    @staticmethod
    def _ass_time(t: str) -> int:
        h, m, s = t.split(":")
        sec, cs = s.split(".")
        return int(h) * 3_600_000 + int(m) * 60_000 + int(sec) * 1000 + int(cs) * 10

    # ---------------------------------------------------------------- 波形加载
    def _start_audio_load(self, video: str, track_index: int | None):
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("正在提取音频并生成波形…")
        self._audio_thread = AudioLoadThread(video, self.cfg, track_index, self)
        self._audio_thread.progress.connect(self._on_audio_progress)
        self._audio_thread.progress_pct.connect(self._on_audio_pct)
        self._audio_thread.finished_ok.connect(self._on_audio_ok)
        self._audio_thread.failed.connect(self._on_audio_fail)
        self._audio_thread.finished.connect(lambda: self.progress.setVisible(False))
        self._audio_thread.start()

    def _on_audio_pct(self, pct: int):
        self.progress.setValue(pct)

    # ---------------------------------------------------------------- 音轨选择
    def _reset_track_combo(self):
        """加载新视频时清空音轨下拉框并禁用。"""
        self._audio_tracks = []
        self._active_track = None
        self.track_combo.blockSignals(True)
        self.track_combo.clear()
        self.track_combo.addItem("自动（默认音轨）", None)
        self.track_combo.blockSignals(False)
        self.track_combo.setEnabled(False)

    def _start_track_probe(self, video: str):
        self._track_thread = TrackProbeThread(video, self.cfg, self)
        self._track_thread.done.connect(self._on_tracks_ready)
        self._track_thread.failed.connect(
            lambda e: self.statusBar().showMessage(f"音轨探测失败：{e}"))
        self._track_thread.start()

    @staticmethod
    def _track_label(t: dict, num: int) -> str:
        """生成音轨下拉项文本，如「音轨 2 · 英语 · 2ch · aac」。"""
        parts = [f"音轨 {num}"]
        lang = t.get("language") or ""
        title = t.get("title") or ""
        name = title or lang
        if name:
            parts.append(name)
        ch = t.get("channels")
        if ch:
            parts.append(f"{ch}ch")
        codec = t.get("codec") or ""
        if codec:
            parts.append(codec)
        return " · ".join(parts)

    def _on_tracks_ready(self, tracks: list[dict]):
        self._audio_tracks = tracks
        self.track_combo.blockSignals(True)
        self.track_combo.clear()
        self.track_combo.addItem("自动（默认音轨）", None)
        for i, t in enumerate(tracks, start=1):
            label = self._track_label(t, i)
            self.track_combo.addItem(label, t.get("stream_index"))
        self.track_combo.blockSignals(False)
        if tracks:
            # 单音轨也显示，但保持默认选择
            self.track_combo.setEnabled(len(tracks) > 1)
            if len(tracks) > 1:
                self.statusBar().showMessage(
                    f"检测到 {len(tracks)} 条音轨，可在工具栏切换（用于波形与对齐）")
        else:
            self.track_combo.setEnabled(False)

    def _on_track_changed(self):
        """用户切换音轨：重新生成波形 + 联动播放音轨。"""
        new_track = self.track_combo.currentData()
        if not self.video_path:
            return
        if new_track == self._active_track:
            return
        # 联动播放音轨：下拉框顺序 = 第 i 条音频流 = Qt 音轨序号 i-1
        idx = self.track_combo.currentIndex()
        self._play_track = (idx - 1) if idx > 0 else None
        self.player.set_active_audio_track(self._play_track)
        # 重新生成波形
        if self._audio_thread and self._audio_thread.isRunning():
            self.statusBar().showMessage("波形生成中，稍后切换音轨会生效")
            return
        self._active_track = new_track
        self._start_audio_load(self.video_path, new_track)
        if new_track is None:
            self.statusBar().showMessage("已切回默认音轨，正在重新生成波形…")
        else:
            self.statusBar().showMessage("音轨已切换，正在重新生成波形…")

    def _on_audio_progress(self, msg: str):
        self.progress.setFormat(msg)

    def _debug_log(self, text: str):
        """把诊断信息追加到 exe 同目录 subsync_debug.log。"""
        if getattr(sys, "frozen", False):
            log_path = Path(sys.executable).resolve().parent / "subsync_debug.log"
        else:
            log_path = Path(__file__).resolve().parents[2] / "subsync_debug.log"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except Exception:
            pass

    def _on_audio_ok(self, mins, maxs, dur_s, block_ms):
        self.waveform.set_peaks(mins, maxs, block_ms)
        # 时长探测失败（<=0）时：用 Qt 的时长兜底，保证波形显示与对齐可用
        if dur_s <= 0:
            dur_s = self.duration_ms / 1000.0
        if dur_s > 0:
            self.waveform.set_media_duration(dur_s)
            self.duration_ms = int(dur_s * 1000)
        # 诊断日志
        try:
            peak = float(max(np.abs(mins).max() if len(mins) else 0.0,
                             np.abs(maxs).max() if len(maxs) else 0.0))
        except Exception:
            peak = 0.0
        self._debug_log(
            f"波形完成: 块数={len(mins)} block_ms={block_ms} dur_s={dur_s:.2f} "
            f"峰值={peak:.0f} 视频={self.video_path}"
        )
        if len(mins) == 0:
            self.statusBar().showMessage("波形生成失败：未提取到音频数据（已写 subsync_ffmpeg_error.log）")
            return
        if peak <= 0:
            self.statusBar().showMessage("波形已生成（音频音量极低/静音，波形为平线）")
        else:
            self.statusBar().showMessage("波形已生成")

    def _on_audio_fail(self, err: str):
        # 把完整错误追加写入 exe 同目录的日志文件，方便事后排查
        self._debug_log(f"波形失败: {err} 视频={self.video_path}")
        if getattr(sys, "frozen", False):
            log_path = Path(sys.executable).resolve().parent / "subsync_ffmpeg_error.log"
        else:
            log_path = Path(__file__).resolve().parents[2] / "subsync_ffmpeg_error.log"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                f.write(f"video: {self.video_path}\n")
                f.write(f"track: {self.track_combo.currentData() if self.track_combo.isEnabled() else None}\n")
                f.write(f"err:\n{err}\n")
        except Exception:
            pass
        msg = f"波形生成失败（已写入日志：{log_path.name}）"
        self.statusBar().showMessage(msg)
        QMessageBox.warning(
            self, "波形生成失败",
            f"提取音频失败：\n\n{err}\n\n"
            f"—— 完整日志已追加到：\n{log_path}\n\n"
            f"可能原因：\n"
            f"1. 视频无音轨\n"
            f"2. 音轨编码不被 ffmpeg 支持（极罕见编码）\n"
            f"3. ffmpeg/ffprobe 异常或被杀毒拦截\n"
            f"4. 路径含特殊字符或权限不足")

    # ---------------------------------------------------------------- 播放联动
    def _on_play_pos(self, ms: int):
        sec = ms / 1000.0
        self.waveform.set_play_position(sec)
        idx = self._subtitle_at(ms)
        if idx >= 0:
            self.panel.highlight_playing(idx)
            self.waveform.select_subtitle(idx)
        self._update_overlay(ms)

    def _update_overlay(self, ms: int):
        idx = self._subtitle_at(ms)
        if idx >= 0:
            self.player.set_subtitle_text(self.subs[idx].text)
        else:
            self.player.set_subtitle_text("")

    def _on_duration(self, ms: int):
        if ms > 0:
            self.duration_ms = ms
            self.waveform.set_media_duration(ms / 1000.0)

    def _subtitle_at(self, ms: int) -> int:
        # 二分查找，字幕数多时更快
        if not self.subs:
            return -1
        lo, hi = 0, len(self.subs) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s = self.subs[mid]
            if s.start_ms <= ms <= s.end_ms:
                return mid
            if ms < s.start_ms:
                hi = mid - 1
            else:
                lo = mid + 1
        return -1

    def _seek_to(self, sec: float):
        self.player.seek_ms(int(sec * 1000))

    def _jump_and_play(self, idx: int):
        if not (0 <= idx < len(self.subs)):
            return
        s = self.subs[idx]
        self.player.seek_ms(s.start_ms)
        self.player.play()
        self.waveform.select_subtitle(idx)
        self.panel.select_index(idx)

    # ---------------------------------------------------------------- 编辑联动
    def _on_wave_drag(self, idx: int, ns: int, ne: int):
        self.panel.refresh_row(idx)
        self._update_overlay(self.player.current_ms())

    def _on_wave_drag_finished(self, idx: int, ns: int, ne: int):
        self.panel.select_index(idx)

    def _on_panel_edit(self, idx: int, ns: int, ne: int):
        self.waveform.update()
        self._update_overlay(self.player.current_ms())

    def _on_panel_text_edit(self, idx: int, text: str):
        self.waveform.update()
        self._update_overlay(self.player.current_ms())

    # ---------------------------------------------------------------- 撤销 / 重做
    def _save_undo_state(self, idx: int = -1):
        """在字幕被修改前调用，把当前状态压入撤销栈（并清空重做栈）。"""
        if not self.subs:
            return
        # 避免连续保存完全相同的状态
        if self._undo_stack:
            last = self._undo_stack[-1]
            if len(last) == len(self.subs):
                same = all(
                    a.index == b.index and a.start_ms == b.start_ms
                    and a.end_ms == b.end_ms and a.text == b.text
                    for a, b in zip(last, self.subs)
                )
                if same:
                    return
        self._undo_stack.append(copy.deepcopy(self.subs))
        if len(self._undo_stack) > self._undo_max:
            self._undo_stack.pop(0)
        # 新操作使旧重做失效
        self._redo_stack.clear()
        self.statusBar().showMessage(f"已记录撤销点（共 {len(self._undo_stack)} 步）")

    def undo(self):
        """Ctrl+Z：恢复到上一个保存的状态。"""
        if not self._undo_stack:
            self.statusBar().showMessage("没有可撤销的操作")
            return
        self._redo_stack.append(copy.deepcopy(self.subs))
        old_subs = self._undo_stack.pop()
        self._restore_subs(old_subs)
        self.statusBar().showMessage(f"已撤销（还可重做 {len(self._redo_stack)} 步）")

    def redo(self):
        """Ctrl+Y：重做被撤销的操作。"""
        if not self._redo_stack:
            self.statusBar().showMessage("没有可重做的操作")
            return
        self._undo_stack.append(copy.deepcopy(self.subs))
        new_subs = self._redo_stack.pop()
        self._restore_subs(new_subs)
        self.statusBar().showMessage(f"已重做（撤销栈 {len(self._undo_stack)} 步）")

    def _restore_subs(self, subs: list[SubtitleLine]):
        self.subs = subs
        self.waveform.set_subtitles(self.subs)
        self.panel.set_subtitles(self.subs)
        self._update_overlay(self.player.current_ms())

    # ---------------------------------------------------------------- 字幕编辑（快捷键）
    def _current_sub_index(self) -> int:
        """当前选中的字幕索引（表格或波形）。"""
        idx = self.panel.current_index()
        if 0 <= idx < len(self.subs):
            return idx
        return -1

    def _refresh_after_edit(self, select: int = -1):
        """编辑字幕列表后统一刷新表格/波形/浮层。"""
        self.waveform.set_subtitles(self.subs)
        self.panel.set_subtitles(self.subs)
        if select >= 0:
            self.panel.select_index(select)
            self.waveform.select_subtitle(select)
        self._update_overlay(self.player.current_ms())

    def nudge_current_subtitle(self, delta_ms: int):
        """Alt+左右：微调当前字幕时间（整体平移该句 ±delta_ms）。"""
        idx = self._current_sub_index()
        if idx < 0:
            self.statusBar().showMessage("请先选中一个字幕再微调时间（Alt+←/→）")
            return
        self._save_undo_state()
        s = self.subs[idx]
        if s.start_ms + delta_ms < 0:
            return
        s.start_ms += delta_ms
        s.end_ms += delta_ms
        self.panel.refresh_row(idx)
        self.waveform.update()
        self._update_overlay(self.player.current_ms())
        self.statusBar().showMessage(
            f"字幕 {idx + 1} 时间 {('+' if delta_ms >= 0 else '')}{delta_ms} ms")

    def shift_all_subtitles(self, delta_ms: int):
        """Ctrl+Shift+左右：全部字幕整体平移 ±delta_ms（对齐微调）。"""
        if not self.subs:
            return
        if self.subs[0].start_ms + delta_ms < 0:
            return
        self._save_undo_state()
        for s in self.subs:
            s.start_ms += delta_ms
            s.end_ms += delta_ms
        self._refresh_after_edit(select=self._current_sub_index())
        self.statusBar().showMessage(
            f"全部字幕整体平移 {('+' if delta_ms >= 0 else '')}{delta_ms} ms")

    def insert_subtitle(self):
        """Ctrl+N：在当前选中字幕之后插入一条新字幕。"""
        if not self.subs:
            self.statusBar().showMessage("请先加载字幕")
            return
        idx = self._current_sub_index()
        if idx < 0:
            idx = len(self.subs) - 1
        self._save_undo_state()
        s = self.subs[idx]
        new_start = s.end_ms
        new_sub = SubtitleLine(
            index=0, start_ms=new_start, end_ms=new_start + 2000, text="")
        self.subs.insert(idx + 1, new_sub)
        for i, ln in enumerate(self.subs, 1):
            ln.index = i
        self._refresh_after_edit(select=idx + 1)
        self.statusBar().showMessage(f"已插入新字幕（第 {idx + 2} 条），可直接编辑文本")
        # 聚焦文本编辑框
        self.panel.text_edit.setFocus()

    def delete_subtitle(self):
        """Ctrl+Delete：删除当前选中的字幕。"""
        idx = self._current_sub_index()
        if idx < 0:
            self.statusBar().showMessage("请先选中要删除的字幕（Ctrl+Delete）")
            return
        self._save_undo_state()
        del self.subs[idx]
        for i, ln in enumerate(self.subs, 1):
            ln.index = i
        self._refresh_after_edit(select=min(idx, len(self.subs) - 1))
        self.statusBar().showMessage(f"已删除字幕（剩余 {len(self.subs)} 条）")

    def jump_to_subtitle(self, offset: int):
        """Alt+上下：跳转到当前句的前 offset(-1)/后 offset(+1) 句并播放。"""
        if not self.subs:
            return
        idx = self._current_sub_index()
        if idx < 0:
            idx = 0
        target = max(0, min(len(self.subs) - 1, idx + offset))
        if target == idx and offset != 0:
            return
        self._jump_and_play(target)

    def play_from_current_subtitle(self):
        """G：从当前选中字幕的开始时间播放。"""
        idx = self._current_sub_index()
        if idx < 0:
            return
        self._jump_and_play(idx)

    # ---------------------------------------------------------------- 快捷键（可自定义）
    MOD_MASK = Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier | Qt.MetaModifier

    def _build_shortcut_map(self):
        """把 id->按键序列 编译成 (key, mods) -> (id, handler, edit_override)。"""
        handlers = self._shortcut_handlers()
        self._shortcut_map = {}
        for sid, seq in self._shortcuts.items():
            parsed = parse_key_sequence(seq)
            if parsed is None:
                continue
            key, mods = parsed
            handler = handlers.get(sid)
            override = DEFAULT_SHORTCUTS[sid][2] if sid in DEFAULT_SHORTCUTS else False
            self._shortcut_map[(int(key), mods_to_int(mods))] = (sid, handler, override)

    def _shortcut_handlers(self) -> dict:
        """功能 id -> 处理函数。"""
        return {
            "toggle_play": self._h_toggle_play,
            "seek_back_1s": lambda: self._seek_relative(-1000),
            "seek_fwd_1s": lambda: self._seek_relative(1000),
            "seek_back_5s": lambda: self._seek_relative(-5000),
            "seek_fwd_5s": lambda: self._seek_relative(5000),
            "seek_back_10s": lambda: self._seek_relative(-10000),
            "seek_fwd_10s": lambda: self._seek_relative(10000),
            "jump_start": lambda: self.player.seek_ms(0),
            "jump_end": self._h_jump_end,
            "play_current": self.play_from_current_subtitle,
            "prev_subtitle": lambda: self.jump_to_subtitle(-1),
            "next_subtitle": lambda: self.jump_to_subtitle(1),
            "nudge_back_100": lambda: self.nudge_current_subtitle(-100),
            "nudge_fwd_100": lambda: self.nudge_current_subtitle(100),
            "nudge_back_500": lambda: self.nudge_current_subtitle(-500),
            "nudge_fwd_500": lambda: self.nudge_current_subtitle(500),
            "shift_all_back": lambda: self.shift_all_subtitles(-500),
            "shift_all_fwd": lambda: self.shift_all_subtitles(500),
            "undo": self.undo,
            "redo": self.redo,
            "insert_subtitle": self.insert_subtitle,
            "delete_subtitle": self.delete_subtitle,
            "open_video": self.open_video,
            "open_srt": self.open_srt,
            "export_srt": self.export_srt,
            "start_align": self._h_start_align,
            "fit_view": lambda: self.waveform.fit_all(),
            "show_help": self.show_shortcut_help,
            "show_settings": self.open_shortcut_settings,
        }

    def _h_toggle_play(self):
        if self.player.has_source():
            self.player.toggle_play()

    def _h_jump_end(self):
        if self.duration_ms > 0:
            self.player.seek_ms(max(0, self.duration_ms - 3000))

    def _h_start_align(self):
        if self.btn_align.isEnabled():
            self.start_align()
        else:
            self.statusBar().showMessage("请先打开视频和字幕，再按 F5 自动对齐")

    def _in_text_editor(self) -> bool:
        return isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit))

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers() & self.MOD_MASK
        entry = self._shortcut_map.get((int(key), mods_to_int(mods)))
        if entry is None:
            super().keyPressEvent(event)
            return
        _, handler, edit_override = entry
        if handler is None:
            super().keyPressEvent(event)
            return
        # 文本编辑框：默认放行给文字编辑；仅标记为“编辑时也生效”的快捷键例外
        if self._in_text_editor() and not edit_override:
            super().keyPressEvent(event)
            return
        handler()

    # ---------------------------------------------------------------- 快捷键设置
    def open_shortcut_settings(self):
        dlg = ShortcutDialog(self._shortcuts, self)
        dlg.saved.connect(self._on_shortcuts_saved)
        dlg.exec()

    def _on_shortcuts_saved(self, shortcuts: dict[str, str]):
        self._shortcuts = dict(shortcuts)
        self._build_shortcut_map()
        save_config(self._shortcuts, self.waveform.wave_height())
        self.statusBar().showMessage("快捷键已更新并保存（配置文件：subsync_settings.json）")

    def _on_wave_height_changed(self, h: int):
        """波形高度调节停止后（500ms 防抖）保存到配置。"""
        save_config(self._shortcuts, h)
        self.statusBar().showMessage(f"波形高度已设为 {h}px 并保存")

    def _seek_relative(self, delta_ms: int):
        """快退/快进指定毫秒。"""
        if not self.player.has_source():
            return
        pos = self.player.current_ms() + delta_ms
        self.player.seek_ms(max(0, pos))

    def show_shortcut_help(self):
        """F1：快捷键帮助（显示当前绑定）。"""
        order = [
            "toggle_play", "seek_back_1s", "seek_fwd_1s", "seek_back_5s",
            "seek_fwd_5s", "seek_back_10s", "seek_fwd_10s", "jump_start",
            "jump_end", "play_current", "prev_subtitle", "next_subtitle",
            "nudge_back_100", "nudge_fwd_100", "nudge_back_500", "nudge_fwd_500",
            "shift_all_back", "shift_all_fwd", "undo", "redo", "insert_subtitle",
            "delete_subtitle", "open_video", "open_srt", "export_srt",
            "start_align", "fit_view", "show_help", "show_settings",
        ]
        rows = []
        for sid in order:
            if sid not in DEFAULT_SHORTCUTS:
                continue
            name = DEFAULT_SHORTCUTS[sid][0]
            key = self._shortcuts.get(sid, "") or "（未设置）"
            rows.append((key, name))
        html = ['<table cellspacing="4" cellpadding="2" style="font-size:13px;">']
        for k, v in rows:
            html.append(
                f'<tr><td style="text-align:right;font-family:Consolas;'
                f'color:#2f6fed;white-space:nowrap;"><b>{k}</b></td>'
                f'<td>&nbsp;&nbsp;{v}</td></tr>')
        html.append("</table>")
        QMessageBox.information(
            self, "快捷键速查",
            f"<h3 style='margin:4px 0;'>快捷键（F2 可自定义）</h3>"
            f"<div style='color:#666;margin-bottom:6px;'>"
            f"按 F2 打开快捷键设置，双击快捷键列即可重新绑定。</div>" + "".join(html))

    # ---------------------------------------------------------------- 自动对齐
    def start_align(self):
        if not self.video_path or not self.subs:
            QMessageBox.information(self, "提示", "请先打开视频和字幕。")
            return
        if not self.cfg.alass_path:
            QMessageBox.warning(self, "缺少 alass", "未找到 alass-cli.exe，无法自动对齐。")
            return
        if not self.srt_path:
            QMessageBox.information(self, "提示", "请先打开字幕文件，自动对齐需要字幕的原始文件路径。")
            return

        # 备份并生成输出路径
        base = Path(self.srt_path)
        backup = base.with_name(base.stem + "_原始备份.srt")
        out_path = base.with_name(base.stem + "_已对齐.srt")

        # 解析对齐模式：auto 时按"字幕总时长 vs 视频时长"启发式判断
        mode = self.align_mode.currentData() or "auto"
        mode_names = {"auto": "自动判断", "shift": "仅整体平移", "split": "智能分段"}
        if mode == "auto":
            try:
                sub_dur = (self.subs[-1].end_ms - self.subs[0].start_ms) / 1000.0
                vid_dur = self.duration_ms / 1000.0
            except Exception:
                sub_dur = vid_dur = 0.0
            if vid_dur > 0 and sub_dur > 0 and abs(sub_dur - vid_dur) / vid_dur < 0.10:
                mode = "shift"   # 时长接近 -> 整体偏移，整体平移最稳
            else:
                mode = "split"   # 时长差异大 -> 可能有删减，用分段
        mode_tip = {
            "shift": "所有字幕统一平移同一偏移量（适合只差几秒、中间无删减）",
            "split": "alass 分段对齐（适合影片中间有删减/插入）",
        }[mode]

        # 音轨：非默认音轨时，对齐参照 = 该音轨提取出的音频（帧率校正仍以默认音轨优先）
        track = self.track_combo.currentData() if self.track_combo.isEnabled() else None
        track_txt = ""
        if track is not None:
            for i, t in enumerate(self._audio_tracks, start=1):
                if t.get("stream_index") == track:
                    track_txt = f"\n音轨：{self._track_label(t, i)}"
                    break

        ask = QMessageBox.question(
            self, "确认对齐",
            f"将对字幕与视频进行自动对齐：\n\n视频：{os.path.basename(self.video_path)}\n"
            f"字幕：{os.path.basename(self.srt_path)}\n"
            f"对齐模式：{mode_names[mode]}（{mode_tip}）\n{track_txt}\n\n"
            f"对齐结果将写入：\n{out_path.name}\n"
            f"（原字幕不会被覆盖，另有 _原始备份 副本）\n\n"
            f"提示：长电影的分段对齐可能需要 5~20 分钟，\n"
            f"进度条会显示已用时间，期间可随时点「停止」。\n\n是否开始？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ask != QMessageBox.Yes:
            return

        try:
            backup.write_text(format_srt(self.subs), encoding="utf-8-sig")
        except Exception as e:
            QMessageBox.warning(self, "备份失败", f"无法写入备份文件：\n{e}")

        # 若选择了特定音轨，先把它提取为临时 wav 作为 alass 参照
        reference = self.video_path
        self._cleanup_track_wav()
        if track is not None and self.cfg.ffmpeg_path:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                tmp = Path(tempfile.gettempdir()) / f"subsync_track{track}.wav"
                self.progress.setVisible(True)
                self.progress.setRange(0, 0)   # 忙碌动画
                self.progress.setFormat("正在提取所选音轨…")
                reference = extract_audio_wav(
                    self.video_path, self.cfg.ffmpeg_path, track, tmp)
                self._pending_track_wav = reference
            except Exception as e:
                QApplication.restoreOverrideCursor()
                self.progress.setVisible(False)
                QMessageBox.critical(self, "音轨提取失败", f"无法提取所选音轨：\n{e}")
                return
            QApplication.restoreOverrideCursor()

        # 启动异步对齐线程（实时进度 + 日志 + 取消）
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("正在对齐… 0%")
        self.btn_align.setEnabled(False)
        self.btn_cancel_align.setVisible(True)
        self.btn_cancel_align.setEnabled(True)
        self._last_align_mode = mode
        # ---- 对齐期间"活着"反馈的状态变量 ----
        self._align_stage = ""              # 当前阶段名（由 stage 信号更新）
        self._last_real_progress_t = 0.0    # 最近一次收到真实进度的时间
        self._cpu_delta = 0.0               # 60 秒窗口内 alass CPU 增量
        self._cpu_samp_t = 0.0
        self._cpu_last_val = 0.0
        # 心跳：真实进度 + 已用时间 + 窗口标题 + CPU 活体监测
        self._align_start_t = time.time()
        self._align_est_timer = QTimer(self)
        self._align_est_timer.setInterval(300)
        self._align_est_timer.timeout.connect(self._align_estimate_progress)
        self._align_est_timer.start()
        self._align_thread = AlignThread(
            reference, self.srt_path, str(out_path), self.cfg,
            mode=mode, parent=self)
        self._align_thread.progress.connect(self._on_align_progress)
        self._align_thread.stage.connect(self._on_align_stage)
        self._align_thread.log.connect(self._on_align_log)
        self._align_thread.done.connect(self._on_align_done)
        self._align_thread.failed.connect(self._on_align_failed)
        self._align_thread.start()

    def _align_estimate_progress(self):
        """对齐心跳：真实进度 + 已用时间 + 窗口标题 + CPU 活体监测。

        目的：让用户随时能判断 alass 是"在工作"还是"真卡死"。
        - 有真实进度行时：进度条显示真实百分比，时间数字持续跳动；
        - 无真实进度行时：显示估算动画（明确标注"估算"），并提示 CPU 是否活跃；
        - 60 秒无任何进展且 CPU 无增量 → 明确警告"疑似卡死，可点击停止"。
        """
        if self._align_thread is None or not self._align_thread.isRunning():
            if self._align_est_timer.isActive():
                self._align_est_timer.stop()
            return
        elapsed = time.time() - self._align_start_t
        eta = f"{int(elapsed) // 60}分{int(elapsed) % 60:02d}秒"
        stage = getattr(self, "_align_stage", "")
        real_t = getattr(self, "_last_real_progress_t", 0.0)
        has_real = (real_t > 0)

        # ---- CPU 活体监测：每 2 秒采样 alass 进程 CPU 时间增量 ----
        stall_hint = ""
        try:
            now = time.time()
            if now - getattr(self, "_cpu_samp_t", 0.0) >= 2.0:
                self._cpu_samp_t = now
                pid = getattr(self._align_thread, "pid", None)
                if pid:
                    try:
                        cur = psutil.Process(pid).cpu_times()
                        val = cur.user + cur.system
                        self._cpu_delta += val - getattr(self, "_cpu_last_val", val)
                        self._cpu_last_val = val
                    except Exception:
                        pass  # 进程已退出等：忽略
            if self._cpu_delta < 0.5:
                idle_since = now - (real_t if has_real else self._align_start_t)
                if idle_since > 60:
                    stall_hint = "⚠ 已60秒无进展"
        except Exception:
            pass

        # ---- 进度条文案 ----
        cur = self.progress.value()
        if has_real:
            stage_txt = f" · {stage}" if stage else ""
            fmt = f"正在对齐{stage_txt} {cur}% · 已用 {eta}"
            if stall_hint:
                fmt += f" · {stall_hint}（可点击停止）"
            self.progress.setFormat(fmt)
        else:
            # 完全没有真实进度时：给"活着"的估算动画（明确标注估算）
            pct = min(80, int(elapsed / 30 * 100))
            if pct > cur:
                self.progress.setValue(pct)
            fmt = f"正在对齐（估算 {pct}%）· 已用 {eta}"
            if stall_hint:
                fmt += f" · {stall_hint}（可点击停止）"
            else:
                fmt += " · 无进度输出，CPU 活跃即正常"
            self.progress.setFormat(fmt)

        # ---- 窗口标题心跳：即使不看进度条也能确认还活着 ----
        try:
            if self.video_path:
                base = f"字幕时间轴对齐工具 - {os.path.basename(self.video_path)}"
            else:
                base = "字幕时间轴对齐工具 · SubSync GUI"
            self.setWindowTitle(f"{base} · 正在对齐 {cur}% · 已用 {eta}")
        except Exception:
            pass

    def _on_align_progress(self, pct: int):
        # 真实进度：记录时间供卡死判定，直接刷新进度条
        self._last_real_progress_t = time.time()
        self.progress.setValue(pct)
        stage = getattr(self, "_align_stage", "")
        fmt = f"正在对齐 {pct}%"
        if stage:
            fmt = f"正在对齐 · {stage} {pct}%"
        self.progress.setFormat(fmt)

    def _on_align_stage(self, name: str):
        # 阶段切换（提取音频 → 帧率估算 → 同步字幕）
        self._align_stage = name
        self.statusBar().showMessage(f"alass: {name}…")

    def _on_align_log(self, line: str):
        # 状态栏更新做 250ms 限流：alass 日志可能很密集，避免状态栏刷新吃光主线程
        import time as _time
        now = _time.time()
        last = getattr(self, "_align_last_log_t", 0)
        if now - last < 0.25:
            return
        self._align_last_log_t = now
        # 只保留最近 3 行，用 " | " 连接显示
        from collections import deque
        if not hasattr(self, "_align_log_buf") or self._align_log_buf is None:
            self._align_log_buf = deque(maxlen=3)
        self._align_log_buf.append(line.strip())
        if self._align_log_buf:
            self.statusBar().showMessage("alass: " + " | ".join(self._align_log_buf))

    def _cancel_align(self):
        if self._align_thread is not None and self._align_thread.isRunning():
            self._align_thread.request_cancel()
            self.btn_cancel_align.setEnabled(False)
            self.statusBar().showMessage("正在停止对齐…")

    def _cleanup_track_wav(self):
        """删除对齐用的临时音轨 wav。"""
        if self._pending_track_wav:
            try:
                Path(self._pending_track_wav).unlink(missing_ok=True)
            except Exception:
                pass
            self._pending_track_wav = None

    def _reset_align_ui(self):
        """对齐结束后恢复 UI 状态。"""
        self.btn_align.setEnabled(bool(self.video_path and self.subs and self.cfg.alass_path))
        self.btn_cancel_align.setVisible(False)
        self.btn_cancel_align.setEnabled(True)
        self._align_log_buf = None
        if hasattr(self, "_align_est_timer") and self._align_est_timer.isActive():
            self._align_est_timer.stop()
        # 恢复窗口标题（去掉"正在对齐"心跳）
        try:
            if self.video_path:
                self.setWindowTitle(f"字幕时间轴对齐工具 - {os.path.basename(self.video_path)}")
            else:
                self.setWindowTitle("字幕时间轴对齐工具 · SubSync GUI")
        except Exception:
            pass

    def _extract_alass_shifts(self, text: str) -> list[str]:
        """从 alass stdout 里提取 'shifted block ... by ...' 汇总，用于诊断。"""
        import re
        lines = []
        for m in re.finditer(r"shifted block of .*? by\s+([+-]?\d:\d{2}:\d{2}\.\d{3})", text, re.I):
            lines.append(m.group(0).strip())
        return lines

    def _save_alass_log(self, srt_out: str, res: AlassResult) -> str:
        """把 alass 的完整输出保存到 .alass.log，方便事后诊断偏移原因。"""
        log_path = Path(srt_out).with_suffix(".alass.log")
        try:
            header = [
                f"returncode: {res.returncode}",
                f"ok: {res.ok}",
                "--- stdout ---",
                res.stdout or "(empty)",
                "--- stderr ---",
                res.stderr or "(empty)",
            ]
            log_path.write_text("\n".join(header), encoding="utf-8")
            return str(log_path)
        except Exception as e:
            return f"(无法写入日志: {e})"

    def _on_align_failed(self, err: str):
        self._reset_align_ui()
        self.progress.setVisible(False)
        self._cleanup_track_wav()
        QMessageBox.critical(
            self, "对齐失败",
            f"{err}\n\n"
            f"常见原因：\n"
            f"1. 视频或字幕路径含中文/空格\n"
            f"2. 视频无法解码\n"
            f"3. 音频轨道缺失\n"
            f"4. 对白稀少的电影请改用「仅整体平移」")

    def _on_align_done(self, res: AlassResult):
        self._reset_align_ui()
        self._cleanup_track_wav()
        log_path = self._save_alass_log(res.output_path, res)
        if res.ok:
            try:
                content, _ = read_srt_file(res.output_path)
                new_subs = parse_srt(content)
                if not new_subs:
                    raise ValueError("对齐结果为空")
                # 保存对齐前的状态到撤销栈，方便用户一键回退
                self._save_undo_state()
                self.subs = new_subs
                self.waveform.set_subtitles(new_subs)
                self.panel.set_subtitles(new_subs)
                n = len(new_subs)
                mode_msg = ("整体平移模式：所有字幕统一偏移，适合整体差几秒的情况。"
                            if self._last_align_mode == "shift"
                            else "分段对齐模式：按内容分段校正，适合中间有删减的影片。")
                shifts = self._extract_alass_shifts(res.stdout)
                shift_txt = "\n".join(shifts[-6:]) if shifts else "（alass 未输出分段偏移信息）"
                QMessageBox.information(
                    self, "对齐完成",
                    f"✅ 自动对齐完成！共 {n} 条字幕。\n\n"
                    f"本次模式：{mode_msg}\n\n"
                    f"alass 实际偏移决策：\n{shift_txt}\n\n"
                    f"结果已保存到：\n{res.output_path}\n\n"
                    f"诊断日志：\n{log_path}\n\n"
                    f"若发现某段仍不准，可在波形图上拖动字幕色块手动微调。")
            except Exception as e:
                QMessageBox.warning(self, "加载结果失败", f"对齐成功但读取结果失败：\n{e}")
        else:
            QMessageBox.critical(
                self, "对齐失败",
                f"alass 返回错误（代码 {res.returncode}）：\n\n{res.stderr or res.stdout or '未知错误'}\n\n"
                f"常见原因：\n"
                f"1. 视频或字幕路径含中文/空格 —— 请复制到纯英文路径再试\n"
                f"2. 视频无法解码 —— 换用 mp4 格式\n"
                f"3. 音频轨道缺失\n"
                f"4. 对白稀少的电影（如 2001 太空漫游）分段对齐失败 ——\n"
                f"   请把上方“对齐模式”切换为“仅整体平移”后重试\n\n"
                f"诊断日志已保存到：\n{log_path}")

        self.progress.setVisible(False)

    # ---------------------------------------------------------------- 导出
    def export_srt(self):
        if not self.subs:
            QMessageBox.information(self, "提示", "没有可导出的字幕。")
            return
        default = ""
        if self.video_path:
            default = str(Path(self.video_path).with_suffix(".aligned.srt"))
        path, _ = QFileDialog.getSaveFileName(
            self, "导出字幕", default, "字幕文件 (*.srt)")
        if not path:
            return
        try:
            Path(path).write_text(format_srt(self.subs), encoding="utf-8-sig")
            self.statusBar().showMessage(f"已导出：{path}（{len(self.subs)} 条）")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
