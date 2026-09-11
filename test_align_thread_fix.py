# -*- coding: utf-8 -*-
"""离屏验证：AlignThread 闭包 nonlocal 修复 + stdout/stderr 双流读取。"""
import os
import subprocess
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, r"B:\wkbuddy\时间轴对齐工具\subsync_gui")

from PySide6.QtWidgets import QApplication

app = QApplication([])

from ui.main_window import AlignThread
from core.alass_runner import AlassConfig

TMP = tempfile.mkdtemp()
srt_in = os.path.join(TMP, "in.srt")
srt_out = os.path.join(TMP, "out.srt")
with open(srt_in, "w", encoding="utf-8") as f:
    f.write("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
with open(srt_out, "w", encoding="utf-8") as f:
    f.write("1\n00:00:01,500 --> 00:00:02,500\nhello\n")   # 假装 alass 已产出


class FakePipe:
    def __init__(self, lines):
        self.lines = [l.encode() for l in lines]
        self.i = 0

    def readline(self):
        if self.i < len(self.lines):
            l = self.lines[self.i]
            self.i += 1
            return l
        return b""


class FakeProc:
    """模拟 alass：stdout/stderr 各输出若干行（含进度行），随后正常退出。"""

    def __init__(self):
        self.stdout = FakePipe([
            "Parsing input files",
            "M[1/4; something",
            "M[2/4; something",
        ])
        self.stderr = FakePipe([
            "M[3/4; something",
            "M[4/4; something",
            "finished in 123.45s",
        ])
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


proc = FakeProc()
_real_popen = subprocess.Popen
subprocess.Popen = lambda *a, **k: proc

events = {"progress": [], "log": [], "done": None, "failed": None}

cfg = AlassConfig(alass_path="X:\\fake\\alass-cli.exe", ffmpeg_path="", ffprobe_path="")
t = AlignThread("ref.mp4", srt_in, srt_out, cfg, mode="split")
t.progress.connect(lambda p: events["progress"].append(p))
t.log.connect(lambda l: events["log"].append(l))
t.done.connect(lambda r: events.update(done=r))
t.failed.connect(lambda e: events.update(failed=e))
t.run()

subprocess.Popen = _real_popen
# 信号是跨线程排队投递的，需要处理事件循环才能送达
for _ in range(10):
    app.processEvents()

print("progress:", events["progress"])
print("log lines:", len(events["log"]), events["log"])
print("failed:", events["failed"])
print("done.ok:", events["done"].ok if events["done"] else None)

# 断言：进度行被解析（不再 UnboundLocalError 崩溃），完成事件正常发出
assert events["failed"] is None, f"不应失败: {events['failed']}"
assert events["done"] is not None and events["done"].ok, "应正常完成"
assert 100 in events["progress"], f"应发出 100% 完成进度: {events['progress']}"
assert len(events["log"]) == 6, f"stdout+stderr 共 6 行都应被读取: {events['log']}"
print("ALL OK: 闭包 nonlocal 修复生效，stdout/stderr 双流均被读取，进度正常")
