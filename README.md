# Subtitle Sync GUI

免安装的 Windows 字幕调轴工具：加载视频 + SRT/ASS 字幕，自动对齐时间轴。

项目地址：<https://github.com/Attyeek/subtitle-sync-gui>

本项目由 `subtitle-sync-gui Authors` 维护，欢迎提交 Issue 和 Pull Request。

- 波形图 + 字幕时间轴（参考 Subtitle Edit 布局）
- 视频画面内实时浮层字幕
- 一键自动对齐（基于 [alass](https://github.com/kaegi/alass)，支持整体平移 / 智能分段 / 自动判断三种模式）
- 多音轨选择：多音轨视频可指定用于波形与对齐的音频轨
- 快捷键自定义（F2），参考 Subtitle Edit / Aegisub
- 编码自动检测（UTF-8 / GBK / GB18030 / Big5）
- 支持拖拽加载，支持 mp4 / mkv / avi / rmvb 等常见格式

## 下载与使用

普通用户不需要安装 Python。请前往 GitHub 的 [Releases](https://github.com/Attyeek/subtitle-sync-gui/releases) 下载最新 Windows 压缩包，解压后运行其中的 exe。

直接运行 `dist\字幕时间轴对齐工具.exe`（单文件，全自包含，无需安装 Python 或任何依赖）。

> Windows 提示"来自未知发布者"时，点「更多信息 → 仍要运行」即可。
> 修改快捷键 / 波形高度后会在 exe 同目录生成 `subsync_settings.json`，想带走配置请一并拷贝。

### 快捷键速查

| 按键 | 功能 |
|---|---|
| 空格 | 播放 / 暂停 |
| ← / → | 快退 / 快进 1 秒 |
| Shift+←/→ | 快退 / 快进 5 秒 |
| Ctrl+←/→ | 快退 / 快进 10 秒 |
| Alt+↑/↓ | 上一条 / 下一条字幕 |
| Alt+←/→ | 当前字幕时间 ±0.1s |
| Ctrl+Shift+←/→ | 全部字幕整体平移 ±0.5s |
| Ctrl+Z / Ctrl+Y | 撤销 / 重做 |
| Ctrl+N / Ctrl+Delete | 插入 / 删除字幕 |
| F5 | 一键自动对齐 |
| F1 / F2 | 快捷键帮助 / 快捷键设置 |

完整列表见软件内 F1。

## 从源码运行

```bash
pip install -r requirements.txt
python subsync_gui/main.py
```

需要 [alass-cli](https://github.com/kaegi/alass/releases) 与 ffmpeg/ffprobe 用于自动对齐与波形：
程序会依次查找 PyInstaller 捆绑目录 → 环境变量 `ALASS_BIN` → PATH → 常见目录（`F:\alass.batch-bat` 等）。

## 打包 exe

```bash
pip install pyinstaller
# 设置 alass 工具目录（可选，默认 F:\alass.batch-bat）
set ALASS_SRC=F:\alass.batch-bat
pyinstaller build.spec
# 产物：dist\字幕时间轴对齐工具.exe
```

打包说明：`build.spec` 会把 alass-cli.exe 与 ffmpeg/ffprobe 一起捆绑进单文件 exe，因此产物约 144MB，换电脑无需额外配置。

## 目录结构

```
subsync_gui/
  main.py                 # 入口
  core/
    srt_parser.py         # SRT 解析/写入 + 编码自动检测
    audio_extract.py      # ffmpeg 提取音频（多音轨）、音轨探测
    waveform.py           # 波形峰值计算
    alass_runner.py       # alass-cli 调用封装
    config.py             # 快捷键表 / 配置读写
  ui/
    main_window.py        # 主窗口、快捷键分发
    video_player.py       # 视频播放 + 字幕浮层（QGraphicsVideoItem）
    waveform_widget.py    # 波形时间轴控件
    subtitle_panel.py     # 字幕列表面板
    shortcut_dialog.py    # 快捷键设置对话框
build.spec                # PyInstaller 打包配置
```

## 许可证

本项目源码采用 [MIT License](LICENSE)。

项目打包和运行涉及的 alass、FFmpeg、PySide6、NumPy、psutil 等第三方组件，分别遵循各自许可证。详见 [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)。

## 常见问题

- **对齐后整片乱掉**：对白稀少的电影（如 2001 太空漫游）请把「对齐模式」切为「仅整体平移」。
- **字幕显示乱码**：已内置编码自动检测；若仍异常请反馈。
- **多音轨视频对齐失败**：在工具栏「音轨」下拉框里选择包含对白的音轨。
