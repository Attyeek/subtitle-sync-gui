# -*- coding: utf-8 -*-
"""SRT 字幕解析与写入。

支持：BOM、CRLF/LF、行号、标准时间码 HH:MM:SS,mmm --> HH:MM:SS,mmm、
多行文本。所有时间内部统一用毫秒 int 表示。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TIME_RE = re.compile(
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)


@dataclass
class SubtitleLine:
    index: int = 0          # 在文件中的序号（1-based）
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""          # 多行文本，行间用 \n
    # 附加信息
    _raw: str = field(default="", repr=False)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def _parse_time(h: str, m: str, s: str, ms: str) -> int:
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1000 + int(ms.ljust(3, "0"))


def parse_srt(content: str) -> list[SubtitleLine]:
    """把 SRT 文本解析成 SubtitleLine 列表。"""
    # 去掉 BOM
    if content.startswith("\ufeff"):
        content = content[1:]
    # 统一换行
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    lines: list[SubtitleLine] = []
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        block_lines = block.strip().split("\n")
        if not block_lines:
            continue
        # 找到时间码所在行
        time_idx = None
        for i, ln in enumerate(block_lines):
            if "-->" in ln and TIME_RE.search(ln):
                time_idx = i
                break
        if time_idx is None:
            continue
        m = TIME_RE.search(block_lines[time_idx])
        if not m:
            continue
        start = _parse_time(*m.groups()[:4])
        end = _parse_time(*m.groups()[4:])
        # 序号：时间码前的数字行（若存在）
        index = 0
        if time_idx >= 1 and block_lines[0].strip().isdigit():
            index = int(block_lines[0].strip())
        # 文本：时间码之后的剩余行
        text = "\n".join(block_lines[time_idx + 1:]).strip()
        lines.append(SubtitleLine(index=index, start_ms=start, end_ms=end,
                                  text=text, _raw=block))
    # 补齐序号
    for i, ln in enumerate(lines):
        if ln.index <= 0:
            ln.index = i + 1
    return lines


def _fmt_ms(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms_ = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms_:03d}"


def format_srt(lines: list[SubtitleLine]) -> str:
    """把 SubtitleLine 列表序列化为 SRT 文本。"""
    parts = []
    for i, ln in enumerate(lines, start=1):
        parts.append(f"{i}\n{_fmt_ms(ln.start_ms)} --> {_fmt_ms(ln.end_ms)}\n{ln.text}")
    return "\n\n".join(parts) + "\n"


# 常见中文乱码字符集合（UTF-8 误读 GBK 或 GBK 误读 UTF-8 时容易出现）
_GARBAGE_CHARS = set("锟斤拷烫握氦烩拢掳茅谩帽赂莽茫碌莽氓锚摹贸枚霉萌朦孟猛梦迷弥秘觅绵苗")


def _score_text(text: str, encoding: str) -> int:
    """对解码后的文本打分，越高越可能是正确编码。"""
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    repl = text.count("\ufffd")
    garbage = sum(text.count(c) for c in _GARBAGE_CHARS)
    # 中文奖励；替换符/乱码字符重罚；UTF 编码略优先（同分时更倾向 UTF-8）
    base = cn * 10 - repl * 2000 - garbage * 100
    if encoding.lower().startswith("utf"):
        base += 5
    return base


def read_srt_file(path: str | Path) -> tuple[str, str]:
    """智能检测编码并读取 SRT 文件。

    依次尝试 UTF-8-sig / UTF-8 / GB18030 / GBK / Big5，按中文字符数量
    和乱码字符数量打分，返回 (content, detected_encoding)。
    如果全部失败，用 latin-1 兜底，保证至少能读到内容。
    """
    path = Path(path)
    raw = path.read_bytes()

    # BOM 优先
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8-sig", errors="strict")
        return text, "utf-8-sig"

    candidates = [
        ("utf-8-sig", "strict"),
        ("utf-8", "strict"),
        ("gb18030", "strict"),
        ("gbk", "strict"),
        ("big5", "strict"),
    ]

    results = []
    for enc, errors in candidates:
        try:
            text = raw.decode(enc, errors=errors)
        except (UnicodeDecodeError, LookupError):
            continue
        results.append((enc, text))

    if results:
        # 按评分排序，取最高分
        results.sort(key=lambda item: _score_text(item[1], item[0]), reverse=True)
        return results[0][1], results[0][0]

    # 兜底：latin-1 不会抛异常，每个字节都能映射
    text = raw.decode("latin-1")
    return text, "latin-1"


def timecode_to_ms(tc: str) -> int:
    """把 'HH:MM:SS,mmm' 或 'HH:MM:SS.mmm' 转成毫秒。"""
    m = re.match(r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})", tc.strip())
    if not m:
        raise ValueError(f"无法解析时间码: {tc!r}")
    return _parse_time(*m.groups())


def ms_to_timecode(ms: int) -> str:
    return _fmt_ms(ms)
