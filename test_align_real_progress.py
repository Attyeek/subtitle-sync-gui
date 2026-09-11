# -*- coding: utf-8 -*-
"""回归：alass 真实进度解析（实测格式 `12 / 75 [====>----] 16.00 %`，\r 刷新，位于 stdout）。

验证：
1. 真实进度值被解析出来（中间值 + 99 上限）
2. 阶段信号按 提取音频→帧率估算→同步字幕 顺序触发
3. 进度行不刷 log 信号（主线程不被淹没）
4. 进程 pid 暴露给主线程、psutil 可采样 CPU（活体监测依赖）
"""
import os
import subprocess
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, r"B:\wkbuddy\时间轴对齐工具\subsync_gui")

import psutil
from PySide6.QtWidgets import QApplication

from ui.main_window import AlignThread

app = QApplication([])


class FakePipe:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.i = 0

    def readline(self):
        if self.i < len(self.chunks):
            c = self.chunks[self.i]
            self.i += 1
            return c
        return b""


class FakeProc:
    def __init__(self, chunks, rc=0):
        self.stdout = FakePipe(chunks)
        self.stderr = FakePipe([])
        self._rc = rc
        self.pid = os.getpid()  # 采样本进程 CPU 验证 psutil 通路

    def poll(self):
        return None

    def wait(self, timeout=None):
        return self._rc

    def terminate(self):
        pass


# 从 bench_tmp/fmt.log 提取的真实输出（略作精简），模拟 \r 把多个进度拼成一段
REAL_LINES = [
    b"extracting audio from reference file 'x.wav'...\n",
    b"\r1 / 24 [==>--------------------------------------------------] 4.17 % 8.02/s 3s "
    b"\r2 / 24 [====>-----------------------------------------------] 8.33 % 12.84/s 2s ",
    b"\r12 / 24 [========================>-------------------------] 50.00 % 25.05/s 0s "
    b"\r24 / 24 [====================================================] 100.00 % 27.79/s ",
    b"\nGuessing framerate ratio...\n",
    b"\r1 / 6 [========>------------------------------------------] 16.67 % 697.06/s 0s "
    b"\r6 / 6 [=====================================================] 100.00 % 782.93/s ",
    b"\ninfo: 'reference file FPS/input file FPS' ratio is 1\n",
    b"\nsynchronizing 'x.srt' to reference file 'x.wav'...\n",
    b"\r1 / 75 [>------------------------------------------------] 1.33 % 31152.65/s 0s "
    b"\r45 / 75 [==========================>-------------------] 60.00 % 16478.69/s 0s ",
    b"\r75 / 75 [=================================================] 100.00 % 14956.33/s ",
    b"\nshifted block of 75 subtitles with length 0:04:50.099 by -0:00:02.500\n",
]


def main():
    real_popen = subprocess.Popen
    subprocess.Popen = lambda *a, **k: FakeProc(REAL_LINES, rc=0)
    try:
        out = os.path.join(r"B:\wkbuddy\时间轴对齐工具\bench_tmp", "test_out.srt")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nx\n")

        class Cfg:
            pass

        cfg = Cfg()
        cfg.alass_path = "alass-cli.exe"
        cfg.ffmpeg_path = ""
        cfg.ffprobe_path = ""

        t = AlignThread("ref.wav", "in.srt", out, cfg, "split")
        hits, stages, logs, done = [], [], [], []
        t.progress.connect(hits.append)
        t.stage.connect(stages.append)
        t.log.connect(lambda l: logs.append(l))
        t.done.connect(lambda r: done.append(r))
        t.failed.connect(lambda e: print("FAILED:", e))
        t.start()
        deadline = time.time() + 8
        while t.isRunning() and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()

        print("progress_hits:", hits)
        print("stages:", stages)
        print("log_count:", len(logs), logs[:5])
        print("done_ok:", bool(done), "pid:", t.pid)
        cpu = psutil.Process(t.pid).cpu_times()
        print("cpu_ok:", round(cpu.user + cpu.system, 3), "s")

        assert stages == ["提取音频", "帧率估算", "同步字幕"], stages
        assert 4 in hits, hits                     # 解析到中间真实进度(2/24)
        assert max(hits) >= 99, hits               # 各阶段 100% 行 clamp 到 99
        assert len(set(hits)) >= 3, hits           # 进度确实在变化
        assert len(logs) <= 6, logs                # 进度行不刷 log（仅阶段/信息行）
        assert done, "done 未触发"
        print("ALL PASS")
    finally:
        subprocess.Popen = real_popen


if __name__ == "__main__":
    main()
