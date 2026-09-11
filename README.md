## Subtitle Sync GUI v1.0.0

### 项目简介
Subtitle Sync GUI 是一款基于 Windows 的字幕时间轴自动校正工具，主要用于一键修正因视频帧率不同、片源版本不同、片头片尾长度变化、广告删减、音画延迟或其他原因造成的字幕偏轴、错位和逐渐漂移问题。

用户只需加载视频和字幕文件，工具会自动分析视频中的音频特征，并将字幕时间轴与实际对白节奏进行匹配，生成一份新的已对齐字幕文件。原始字幕不会被覆盖，适合电影、电视剧及不同版本片源之间的字幕同步处理。

#### 核心技术与原理
本项目采用 PySide6 + FFmpeg + alass 构建。

#### 音频提取与分析
工具通过 FFmpeg 从视频中提取音频，并将音频转换为适合分析的 PCM 数据。随后计算音频能量峰值和语音活动特征，生成可视化波形时间轴。

#### 字幕与音频特征匹配
alass 是一种语言无关的字幕同步算法，不依赖具体语言内容，而是通过比较字幕时间分布与视频音频活动区间，寻找两者之间的最佳时间对应关系。

#### 自动修正整体偏移
如果字幕只是整体提前或延后，算法会计算统一的时间偏移量，将所有字幕同步移动到正确位置。

#### 处理帧率差异和时间漂移
当两个片源的帧率不同，例如 23.976 fps 与 25 fps，字幕误差通常会随着播放时间逐渐扩大。工具可以根据前后多个时间点的匹配结果，建立分段时间映射，对不同区间分别进行校正，而不是简单地对整部影片使用同一个固定偏移量。

#### 处理片源删减和版本差异
对于影院版、导演剪辑版、电视版或包含删减片段的不同片源，工具支持智能分段对齐，在必要时调整字幕分段之间的时间关系，尽量减少因片段缺失或新增造成的后续错位。

#### 可视化人工复核
工具提供视频画面、字幕列表、音频波形和红色播放指针。播放时红针会沿时间轴实时向前滚动，用户可以直观看到字幕区间与音频波形的对应关系，并对个别字幕进行手动微调。

### 主要功能

- 一键修正字幕整体偏移
- 处理不同帧率导致的时间漂移
- 处理片源删减、片头片尾差异和音画延迟
- 支持智能分段对齐
- 支持多音轨选择
- 提供音频波形和字幕时间轴可视化
- 播放时红色时间指针实时滚动
- 支持 SRT / ASS 等常见字幕格式

### 使用方式

下载下方 Windows 压缩包，解压后直接运行 exe，无需安装 Python。

### 注意

程序包含 alass、FFmpeg、PySide6、NumPy、psutil 等第三方组件，相关许可信息见压缩包中的 `THIRD-PARTY-LICENSES.md`。


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

- **对齐后整片乱掉**：对白稀少的电影（如 2001：太空漫游）请把「对齐模式」切为「仅整体平移」。
- **字幕显示乱码**：已内置编码自动检测；若仍异常请反馈。
- **多音轨视频对齐失败**：在工具栏「音轨」下拉框里选择包含对白的音轨。
