# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本。

用法（在项目根目录执行）：
    pyinstaller build.spec

生成：dist/字幕时间轴对齐工具.exe

体积优化策略：
  1) 剔除 PySide6 默认带入但用不到的 Qt DLL（Qml/Quick/Pdf 等），约省 18MB。
  2) alass-cli.exe 与 Python/numpy 等二进制用 UPX 压缩（约几 MB）。
  3) ffmpeg 工具不 UPX 压缩：PyInstaller onefile 模式下 zlib 已对 DLL 高度压缩
     （如 avcodec 71MB -> 约 19MB），UPX 在此基础上几乎无收益；且有用户报告
     UPX 后的 ffmpeg 在某些 mp4 上无法解码导致波形缺失，故保守保功能不压缩。
"""
from __future__ import annotations

import os
import shutil
import subprocess

ROOT = os.getcwd()  # 按文档在项目根目录执行 pyinstaller build.spec

# 捆绑 alass 与 ffmpeg，让 exe 单文件即可全自包含运行
ALASS_SRC = os.environ.get("ALASS_SRC", r"F:\alass.batch-bat")
if not os.path.isdir(ALASS_SRC):
    raise SystemExit(f"找不到 alass 工具目录（请检查或设置 ALASS_SRC 环境变量）: {ALASS_SRC}")
ALASS_BIN = os.path.join(ALASS_SRC, "bin", "alass-cli.exe")
FF_BIN = os.path.join(ALASS_SRC, "ffmpeg", "bin")
if not os.path.isfile(ALASS_BIN):
    raise SystemExit(f"找不到 alass-cli.exe: {ALASS_BIN}")
if not os.path.isdir(FF_BIN):
    raise SystemExit(f"找不到 ffmpeg 目录: {FF_BIN}")

# ------------------------------------------------------------------ 准备工具
UPX_EXE = os.environ.get("UPX_PATH", os.path.join(ROOT, "tools", "upx.exe"))


def prepare_tools() -> str:
    """把 alass + ffmpeg 拷贝到 build/tools_stage/，仅对 alass 进行 UPX 压缩。

    ffmpeg 保留原样以确保解码兼容性。
    """
    out_dir = os.path.join(ROOT, "build", "tools_stage")
    ff_out = os.path.join(out_dir, "ffmpeg", "bin")
    alass_out = os.path.join(out_dir, "alass", "cli")
    os.makedirs(ff_out, exist_ok=True)
    os.makedirs(alass_out, exist_ok=True)

    # 拷贝 ffmpeg 完整 bin（保留原样，不压缩）
    for f in os.listdir(FF_BIN):
        src, dst = os.path.join(FF_BIN, f), os.path.join(ff_out, f)
        if not os.path.isfile(dst) or os.path.getsize(src) != os.path.getsize(dst):
            shutil.copy2(src, dst)

    # alass-cli 拷贝 + 单独 UPX
    dst_alass = os.path.join(alass_out, "alass-cli.exe")
    if not os.path.isfile(dst_alass):
        shutil.copy2(ALASS_BIN, dst_alass)
    if os.path.isfile(UPX_EXE):
        try:
            subprocess.run([UPX_EXE, "-q", dst_alass], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    return out_dir


TOOLS = prepare_tools()
datas = [
    (os.path.join(TOOLS, "alass", "cli", "alass-cli.exe"), "alass/cli"),
    (os.path.join(TOOLS, "ffmpeg", "bin"), "alass/ffmpeg/bin"),
]

a = Analysis(
    [os.path.join(ROOT, "subsync_gui", "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "psutil",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.Qt3DCore",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtGraphs",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtQuick3D",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSql",
        "PySide6.QtStateMachine",
        "PySide6.QtTest",
        "PySide6.QtWebChannel",
        "PySide6.QtWebSockets",
        "PySide6.QtXml",
        "PySide6.QtBluetooth",
        "PySide6.QtHelp",
        "PySide6.QtLocation",
        "PySide6.QtMultimediaQuick",
        "PySide6.QtNetworkAuth",
        "PySide6.QtNfc",
        "PySide6.QtOpenGL",
        "PySide6.QtPositioning",
        "PySide6.QtQmlModels",
        "PySide6.QtQuickControls2",
        "PySide6.QtQuickTemplates2",
        "PySide6.QtQuickWidgets",
        "PySide6.QtSvg",
        "PySide6.QtSvgWidgets",
        "PySide6.QtTextToSpeech",
        "PySide6.QtUiTools",
        "PySide6.QtWebView",
    ],
    noarchive=False,
    optimize=1,
)

# 注：之前尝试剔除 Qt6Qml/Quick/Pdf 等"无关"DLL 来缩体积，但用户在 2026-08-25
# 反馈精简后交互异常（工具栏按钮不显示、拖拽异常等），疑似剔除副作用。
# 现回退保留所有 Qt DLL，保证交互稳定。体积回到 ~150M。

a.binaries = a.binaries

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="字幕时间轴对齐工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # Qt 的 DLL 用 UPX 压缩易损坏，保持原样最稳
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
