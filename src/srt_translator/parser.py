"""SRT file parsing and saving utilities."""

from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Optional

from .models import SrtEntry

logger = logging.getLogger(__name__)

TIME_LINE_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*$"
)


def _parse_srt_block_lines(lines: Sequence[str]) -> Optional[SrtEntry]:
    """Parse one SRT block from already-stripped lines."""
    cleaned = [line.strip("\ufeff") for line in lines if line.strip()]
    if len(cleaned) < 3:
        return None
    try:
        idx = int(cleaned[0].strip())
    except ValueError:
        return None
    time_match = TIME_LINE_RE.match(cleaned[1])
    if not time_match:
        return None
    text = " ".join(line.strip() for line in cleaned[2:] if line.strip())
    if not text:
        return None
    start, end = time_match.groups()
    return SrtEntry(idx, start, end, text)


def parse_srt(content: str) -> List[SrtEntry]:
    """
    Parse SRT file content into list of SrtEntry objects.
    
    Args:
        content: Raw SRT file content as string
        
    Returns:
        List of parsed SrtEntry objects
    """
    if not content or not content.strip():
        return []
    
    # 预处理：标准化换行符，确保末尾有空行
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    content = content.strip() + '\n\n'
    
    # 修复后的正则：支持文件末尾匹配
    pattern = (
        r"(\d+)\s*\n"                                              # 序号
        r"\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"                   # 开始时间
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"                          # 结束时间
        r"([\s\S]*?)(?=\n\s*\n\d+\s*\n|\n\s*\n\s*$|\s*$)"          # 文本内容
    )
    
    entries: List[SrtEntry] = []
    
    for match in re.finditer(pattern, content):
        idx, start, end, text = match.groups()
        # 清理多行文本为单行
        clean_text = " ".join(line.strip() for line in text.strip().splitlines() if line.strip())
        if clean_text:  # 只添加有内容的条目
            entries.append(SrtEntry(int(idx), start, end, clean_text))
    
    if not entries:
        logger.warning("No valid SRT entries found in content")
    
    return entries


def iter_srt_entries(path: Path) -> Iterator[SrtEntry]:
    """Stream SRT entries from a file without loading the whole file."""
    block_lines: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline=None) as f:
        for raw_line in f:
            line = raw_line.rstrip("\n\r")
            if line.strip():
                block_lines.append(line)
                continue
            if block_lines:
                entry = _parse_srt_block_lines(block_lines)
                if entry:
                    yield entry
                else:
                    logger.debug("Skipping invalid SRT block: %s", block_lines[:2])
                block_lines = []

    if block_lines:
        entry = _parse_srt_block_lines(block_lines)
        if entry:
            yield entry
        else:
            logger.debug("Skipping invalid SRT block at EOF: %s", block_lines[:2])


def save_srt_iter(entries: Iterable[SrtEntry], path: Path) -> int:
    """Stream SRT entries to disk and return the number written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for count, entry in enumerate(entries, 1):
            f.write(entry.to_srt(count))
    logger.info("Saved %s entries to %s", count, path)
    return count


def validate_srt_file(path: Path, max_size_bytes: int | None = None) -> Optional[str]:
    """
    Validate SRT file before processing.
    
    Args:
        path: Path to SRT file
        
    Returns:
        Error message if invalid, None if valid
    """
    if not path.exists():
        return f"File not found: {path}"
    
    if not path.is_file():
        return f"Not a file: {path}"
    
    suffix = path.suffix.lower()
    if suffix != '.srt':
        return f"Invalid file extension: {suffix} (expected .srt)"
    
    size = path.stat().st_size
    if size == 0:
        return "File is empty"
    if max_size_bytes is not None and size > max_size_bytes:
        return (
            f"File too large: {size / 1024 / 1024:.1f}MB "
            f"(max {max_size_bytes / 1024 / 1024:.1f}MB)"
        )

    try:
        if next(iter_srt_entries(path), None) is None:
            return "No valid SRT entries found"
    except UnicodeDecodeError:
        return "SRT file must be UTF-8 encoded"
    
    return None


def save_srt(entries: Sequence[SrtEntry], path: Path) -> None:
    """
    Save SrtEntry list to SRT file.
    
    Args:
        entries: Sequence of SrtEntry objects to save
        path: Output file path
    """
    # 确保父目录存在
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with path.open("w", encoding="utf-8") as f:
        for new_idx, e in enumerate(entries, 1):
            f.write(e.to_srt(new_idx))
    
    logger.info(f"Saved {len(entries)} entries to {path}")
