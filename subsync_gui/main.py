# -*- coding: utf-8 -*-
"""字幕时间轴对齐工具入口。

运行：
    python main.py
"""
from __future__ import annotations

import os
import sys

# 抑制 QtMultimedia ffmpeg 后端的调试输出，避免弹/保留黑窗日志
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.ffmpeg=false")
# Windows 下禁用 matplotlib/tk 等可能拉起的控制台后端（如果有）
os.environ.setdefault("MPLBACKEND", "Agg")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("SubSync GUI")
    app.setOrganizationName("SubSync")

    font = QFont("Microsoft YaHei", 9)
    app.setFont(font)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
