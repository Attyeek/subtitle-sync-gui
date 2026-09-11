# -*- coding: utf-8 -*-
"""快捷键设置对话框：查看/修改/恢复快捷键。

双击"快捷键"列进入录制模式，按下新按键组合完成绑定；
冲突的按键会被拒绝并提示。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QAbstractItemView, QMessageBox,
)

from core.config import (
    DEFAULT_SHORTCUTS, parse_key_sequence, key_combination_to_str,
)


class ShortcutDialog(QDialog):
    # 保存时发出：{"id": "按键序列", ...}
    saved = Signal(dict)

    COL_NAME = 0
    COL_KEY = 1

    def __init__(self, current: dict[str, str], parent=None):
        """current: id -> 按键序列字符串。"""
        super().__init__(parent)
        self.setWindowTitle("快捷键设置")
        self.setMinimumSize(580, 540)

        self._current = dict(current)   # 编辑态（未保存）
        self._recording_id: str | None = None

        lay = QVBoxLayout(self)

        tip = QLabel(
            "双击某一行的“快捷键”列，然后按下你想绑定的按键组合（如 Ctrl+Shift+Left）。\n"
            "修改后点击“保存”生效；“恢复默认”可重置全部。")
        tip.setStyleSheet("color:#666; padding:2px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["功能", "快捷键"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._on_cell_dbl)
        lay.addWidget(self.table, 1)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color:#2f6fed; font-weight:bold; padding:2px;")
        lay.addWidget(self._hint)

        btns = QHBoxLayout()
        self.btn_default = QPushButton("恢复默认")
        self.btn_default.clicked.connect(self._reset_all)
        btns.addWidget(self.btn_default)
        btns.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btns.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        btn_save.setObjectName("primaryBtn")
        btn_save.clicked.connect(self._on_save)
        btns.addWidget(btn_save)
        lay.addLayout(btns)

        self._build_rows()

    # ------------------------------------------------------------- 数据
    def _build_rows(self):
        self.table.setRowCount(0)
        for sid, (name, _, _) in DEFAULT_SHORTCUTS.items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            it_name = QTableWidgetItem(name)
            it_name.setFlags(it_name.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, self.COL_NAME, it_name)
            it_key = QTableWidgetItem(self._current.get(sid, "") or "（未设置）")
            it_key.setFlags(it_key.flags() & ~Qt.ItemIsEditable)
            it_key.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, self.COL_KEY, it_key)

    def _id_at_row(self, row: int) -> str | None:
        name = self.table.item(row, self.COL_NAME).text()
        for sid, (n, _, _) in DEFAULT_SHORTCUTS.items():
            if n == name:
                return sid
        return None

    def _refresh_row_key(self, sid: str):
        for row in range(self.table.rowCount()):
            if self._id_at_row(row) == sid:
                self.table.item(row, self.COL_KEY).setText(
                    self._current.get(sid, "") or "（未设置）")
                return

    # ------------------------------------------------------------- 交互
    def _on_cell_dbl(self, row: int, col: int):
        if col != self.COL_KEY:
            return
        sid = self._id_at_row(row)
        if not sid:
            return
        self._recording_id = sid
        self.table.selectRow(row)
        self._hint.setText(f"正在录制「{DEFAULT_SHORTCUTS[sid][0]}」… 请按下新的按键组合")

    def keyPressEvent(self, event):
        if self._recording_id is None:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            self._hint.setText("请同时按下组合键（如 Ctrl+Shift+Left）…")
            return
        seq_str = key_combination_to_str(key, event.modifiers())
        if not seq_str:
            self._hint.setText("无法识别的按键，请重试")
            return
        # 冲突检测
        for sid, seq in self._current.items():
            if sid != self._recording_id and seq == seq_str:
                self._hint.setText(
                    f"⚠ 该按键已绑定给「{DEFAULT_SHORTCUTS[sid][0]}」，请换一个组合键")
                return
        self._current[self._recording_id] = seq_str
        self._refresh_row_key(self._recording_id)
        name = DEFAULT_SHORTCUTS[self._recording_id][0]
        self._recording_id = None
        self._hint.setText(f"✅ 「{name}」已绑定为 {seq_str}。可继续双击其他行修改。")

    def _reset_all(self):
        self._current = {k: v[1] for k, v in DEFAULT_SHORTCUTS.items()}
        self._recording_id = None
        self._hint.setText("已恢复为默认快捷键，点击“保存”生效。")
        self._build_rows()

    def _on_save(self):
        seen: dict[str, str] = {}
        for sid, seq in self._current.items():
            if not seq or parse_key_sequence(seq) is None:
                QMessageBox.warning(
                    self, "无效按键",
                    f"「{DEFAULT_SHORTCUTS[sid][0]}」的快捷键无效或未设置，请修改后再保存。")
                return
            if seq in seen:
                QMessageBox.warning(
                    self, "按键冲突",
                    f"「{DEFAULT_SHORTCUTS[seen[seq]][0]}」与「{DEFAULT_SHORTCUTS[sid][0]}」"
                    f"使用了相同按键 {seq}，请修改。")
                return
            seen[seq] = sid
        self.saved.emit(dict(self._current))
        self.accept()
