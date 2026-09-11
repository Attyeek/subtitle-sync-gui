# -*- coding: utf-8 -*-
"""回归：模拟 alass 输出大量 \r 进度条（真实格式），验证不刷屏、不卡主线程。"""
import os
import sys
import time
import threading

sys.path.insert(0, r"B:\wkbuddy\时间轴对齐工具\subsync_gui")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import subprocess
_real_popen = subprocess.Popen


class FakeStdout:
    def __init__(self, chunks):
        self.chunks = chunks
        self.i = 0

    def readline(self):
        if self.i < len(self.chunks):
            c = self.chunks[self.i].encode("utf-8")
            self.i += 1
            return c
        return b""


class FakeProc:
    def __init__(self, out_chunks, err_chunks):
        self.stdout = FakeStdout(out_chunks)
        self.stderr = FakeStdout(err_chunks)
        self._rc = 0
        self._terminated = threading.Event()
        self.pid = os.getpid()

    def wait(self, timeout=None):
        return self._rc

    def terminate(self):
        self._terminated.set()


def fake_popen(*args, **kwargs):
    # 真实格式刷屏：2000 次 \r 进度刷新，分 100 批（模拟管道分块）
    out = ["extracting audio from reference file 'ref.mp4'..."]
    for batch in range(100):
        parts = []
        for j in range(20):
            n = batch * 20 + j + 1
            bar = '=' * (n % 40) + '.' * (39 - n % 40)
            parts.append(f"{n} / 2000 [{bar}] {n / 20:.2f} % 15000/s 0s")
        out.append("\r".join(parts))
    out.append("shifted block of 2000 subtitles by -0:00:02.500")
    return FakeProc(out, [])


subprocess.Popen = fake_popen

from PySide6.QtWidgets import QApplication
from ui.main_window import AlignThread

# 缩短超时，避免测试意外挂死
AlignThread._TIMEOUT = 5.0

app = QApplication.instance() or QApplication([])
log_count = [0]
progress_hits = []


def on_log(line):
    log_count[0] += 1


def on_progress(p):
    progress_hits.append(p)


t = AlignThread("ref.mp4", "in.srt", "out.srt",
                type("C", (), {"alass_path": "alass.exe", "ffmpeg_path": "", "ffprobe_path": ""})(),
                "split")
t.log.connect(on_log)
t.progress.connect(on_progress)

t0 = time.time()
t.start()
# 跑一个小事件循环，等 QThread 结束并投递信号
deadline = time.time() + 5
while t.isRunning() and time.time() < deadline:
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
elapsed = time.time() - t0

print(f"log_count={log_count[0]} (期望 << 2000)")
print(f"progress_hits={len(progress_hits)} 首尾={progress_hits[:3]}...{progress_hits[-3:]}")
print(f"elapsed={elapsed:.3f}s (期望 < 2s)")
ok = log_count[0] < 50 and elapsed < 2.0 and len(progress_hits) >= 50
print(f"ok={ok}")

subprocess.Popen = _real_popen
