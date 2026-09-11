# -*- coding: utf-8 -*-
"""字幕列表面板：表格 + 精确编辑区。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QLabel, QLineEdit, QTextEdit, QPushButton, QHeaderView, QAbstractItemView,
)

from core.srt_parser import SubtitleLine, ms_to_timecode, timecode_to_ms


class SubtitlePanel(QWidget):
    row_selected = Signal(int)               # 字幕索引
    row_double_clicked = Signal(int)         # 双击 -> 跳转播放
    times_edited = Signal(int, int, int)     # index, new_start_ms, new_end_ms
    text_edited = Signal(int, str)           # index, new_text
    edit_started = Signal(int)               # 用户开始对某行进行编辑（用于撤销栈保存旧状态）

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lay.addWidget(QLabel("字幕列表（双击跳转，双击单元格可编辑）"))

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "开始", "结束", "文本"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumWidth(300)
        lay.addWidget(self.table, 1)

        self.table.itemSelectionChanged.connect(self._on_sel)
        self.table.itemDoubleClicked.connect(self._on_item_dbl)
        self.table.itemChanged.connect(self._on_item_edited)

        # 当前字幕文本编辑区（Subtitle Edit 风格）
        lay.addWidget(QLabel("当前字幕文本"))
        self.text_edit = QTextEdit()
        self.text_edit.setMaximumHeight(120)
        self.text_edit.setPlaceholderText("选中字幕后在此编辑内容…")
        self.text_edit.textChanged.connect(self._on_text_changed)
        lay.addWidget(self.text_edit)

        # 时间编辑区
        edit_box = QHBoxLayout()
        edit_box.addWidget(QLabel("开始"))
        self.ed_start = QLineEdit()
        self.ed_start.setPlaceholderText("HH:MM:SS,mmm")
        self.ed_start.setMaximumWidth(110)
        edit_box.addWidget(self.ed_start)
        edit_box.addWidget(QLabel("结束"))
        self.ed_end = QLineEdit()
        self.ed_end.setPlaceholderText("HH:MM:SS,mmm")
        self.ed_end.setMaximumWidth(110)
        edit_box.addWidget(self.ed_end)
        self.btn_apply = QPushButton("应用")
        self.btn_apply.setToolTip("把编辑区的时间应用到当前字幕")
        self.btn_apply.clicked.connect(self._apply_edit)
        edit_box.addWidget(self.btn_apply)
        edit_box.addStretch(1)
        lay.addLayout(edit_box)

        self._subs: list[SubtitleLine] = []
        self._current = -1
        self._suppress = False
        self._text_dirty = False  # 标记当前字幕文本是否已被用户改动过

    # ------------------------------------------------------------- 数据
    def set_subtitles(self, subs: list[SubtitleLine]):
        self._subs = list(subs)
        self._suppress = True
        self.table.setRowCount(len(subs))
        for i, s in enumerate(subs):
            self._fill_row(i, s)
        self._suppress = False
        self._current = 0 if subs else -1
        if subs:
            self.table.selectRow(0)
            self._load_edit(0)
        else:
            self.text_edit.clear()
            self.ed_start.clear()
            self.ed_end.clear()

    def _fill_row(self, row: int, s: SubtitleLine):
        items = [
            QTableWidgetItem(str(s.index)),
            QTableWidgetItem(ms_to_timecode(s.start_ms)),
            QTableWidgetItem(ms_to_timecode(s.end_ms)),
            QTableWidgetItem(s.text.replace("\n", " ⏎ ")),
        ]
        for c, it in enumerate(items):
            if c == 0:
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, c, it)

    def refresh_row(self, index: int):
        if 0 <= index < len(self._subs):
            s = self._subs[index]
            self._suppress = True
            self.table.item(index, 1).setText(ms_to_timecode(s.start_ms))
            self.table.item(index, 2).setText(ms_to_timecode(s.end_ms))
            self.table.item(index, 3).setText(s.text.replace("\n", " ⏎ "))
            self._suppress = False

    def current_index(self) -> int:
        return self._current

    def select_index(self, index: int):
        if 0 <= index < self.table.rowCount():
            self._suppress = True
            self.table.selectRow(index)
            self._suppress = False
            self._current = index
            self._load_edit(index)
            self.table.scrollToItem(self.table.item(index, 0))

    def highlight_playing(self, index: int):
        """播放时高亮当前句（并自动滚动）。"""
        if index < 0 or index >= self.table.rowCount():
            return
        if index == self._current and self.table.selectionModel().isSelected(
                self.table.model().index(index, 0)):
            return
        self._suppress = True
        self.table.selectRow(index)
        self._suppress = False
        self._current = index
        self._load_edit(index)
        self.table.scrollToItem(self.table.item(index, 0))

    # ------------------------------------------------------------- 交互
    def _on_sel(self):
        if self._suppress:
            return
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        self._current = idx
        self._load_edit(idx)
        self.row_selected.emit(idx)

    def _on_item_dbl(self, item):
        if self._suppress:
            return
        idx = item.row()
        self.edit_started.emit(idx)
        self.row_double_clicked.emit(idx)

    def _on_item_edited(self, item):
        if self._suppress:
            return
        idx = item.row()
        col = item.column()
        s = self._subs[idx]
        try:
            if col == 1:
                s.start_ms = timecode_to_ms(item.text())
            elif col == 2:
                s.end_ms = timecode_to_ms(item.text())
            elif col == 3:
                s.text = item.text().replace(" ⏎ ", "\n")
                self._suppress = True
                self.text_edit.setPlainText(s.text)
                self._suppress = False
            else:
                return
        except ValueError:
            # 非法输入，回滚显示
            self.refresh_row(idx)
            return
        if s.end_ms <= s.start_ms:
            s.end_ms = s.start_ms + 200
            self.refresh_row(idx)
        self.times_edited.emit(idx, s.start_ms, s.end_ms)

    def _load_edit(self, idx: int):
        s = self._subs[idx]
        self._suppress = True
        self.text_edit.setPlainText(s.text)
        self.ed_start.setText(ms_to_timecode(s.start_ms))
        self.ed_end.setText(ms_to_timecode(s.end_ms))
        self._text_dirty = False
        self._suppress = False

    def _apply_edit(self):
        if not (0 <= self._current < len(self._subs)):
            return
        self.edit_started.emit(self._current)
        s = self._subs[self._current]
        try:
            ns = timecode_to_ms(self.ed_start.text())
            ne = timecode_to_ms(self.ed_end.text())
        except ValueError:
            self._load_edit(self._current)
            return
        if ne <= ns:
            ne = ns + 200
        s.start_ms, s.end_ms = ns, ne
        s.text = self.text_edit.toPlainText()
        self.refresh_row(self._current)
        self.times_edited.emit(self._current, ns, ne)
        self.text_edited.emit(self._current, s.text)

    def _on_text_changed(self):
        if self._suppress or not (0 <= self._current < len(self._subs)):
            return
        # 第一次用户输入时通知外部保存撤销状态
        if not self._text_dirty:
            self._text_dirty = True
            self.edit_started.emit(self._current)
        text = self.text_edit.toPlainText()
        s = self._subs[self._current]
        if s.text != text:
            s.text = text
            self._suppress = True
            self.table.item(self._current, 3).setText(text.replace("\n", " ⏎ "))
            self._suppress = False
            self.text_edited.emit(self._current, text)
