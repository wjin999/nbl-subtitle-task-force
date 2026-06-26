"""Glossary loading and management utilities."""

from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class Glossary:
    """术语表管理类，使用编译正则实现高效匹配。"""

    def __init__(self):
        self._terms: Dict[str, str] = {}       # 原始大小写 -> 翻译
        self._lower_to_orig: Dict[str, str] = {}  # 小写 -> 原始大小写
        self._pattern: re.Pattern | None = None   # 编译后的正则（惰性构建）

    def add(self, term: str, translation: str) -> None:
        """添加术语。"""
        term = term.strip()
        translation = translation.strip()
        if term and translation:
            self._terms[term] = translation
            self._lower_to_orig[term.lower()] = term
            self._pattern = None  # 使缓存失效

    def _build_pattern(self) -> re.Pattern:
        """构建匹配所有术语的正则模式（大小写不敏感）。"""
        escaped = [re.escape(t) for t in self._terms]
        # 按长度降序排列，确保长术语优先匹配
        escaped.sort(key=len, reverse=True)
        return re.compile('|'.join(escaped), re.IGNORECASE)

    def get(self, term: str) -> str | None:
        """获取术语翻译（不区分大小写）。"""
        original_term = self._lower_to_orig.get(term.lower())
        if original_term:
            return self._terms.get(original_term)
        return None

    def find_matches(self, text: str) -> Dict[str, str]:
        """
        在文本中查找匹配的术语（单次正则扫描，O(n)）。

        返回原始大小写的术语及其翻译。
        """
        if not self._terms or not text:
            return {}
        if self._pattern is None:
            self._pattern = self._build_pattern()
        matches: Dict[str, str] = {}
        for m in self._pattern.finditer(text):
            matched_lower = m.group().lower()
            orig = self._lower_to_orig.get(matched_lower)
            if orig and orig not in matches:
                matches[orig] = self._terms[orig]
        return matches

    def __len__(self) -> int:
        return len(self._terms)

    def __bool__(self) -> bool:
        return len(self._terms) > 0

    def items(self):
        """Return glossary term pairs."""
        return self._terms.items()

    def to_dict(self) -> Dict[str, str]:
        """Return a plain dictionary copy."""
        return dict(self._terms)


def _parse_glossary_lines(lines: list[str]) -> Glossary:
    """从行列表解析术语表。

    Supported formats:
        Term = Translation
        Term -> Translation
        Term: Translation
        # Comment lines

    Args:
        lines: List of text lines to parse

    Returns:
        Glossary instance
    """
    glossary = Glossary()
    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # 优先匹配较长的分隔符 ->，然后 =，最后 :
        match = re.match(r'^(.+?)\s*->\s*(.+)$', line)
        if not match:
            match = re.match(r'^(.+?)\s*=\s*(.+)$', line)
        if not match:
            match = re.match(r'^(.+?)\s*:\s*(.+)$', line)
        if match:
            term, translation = match.groups()
            glossary.add(term, translation)
        else:
            logger.debug(f"Skipping invalid line {line_num}: {line}")
    return glossary


def load_glossary(path: Path) -> Glossary:
    """
    Load glossary from a text file.
    
    Supported formats:
        Term = Translation
        Term -> Translation
        Term: Translation
        # Comment lines
    
    Args:
        path: Path to glossary file
        
    Returns:
        Glossary instance
    """
    glossary = Glossary()
    
    if not path.exists():
        logger.warning(f"Glossary file not found: {path}")
        return glossary
    
    try:
        content = path.read_text(encoding='utf-8')
        glossary = _parse_glossary_lines(content.splitlines())
    except Exception as e:
        logger.error(f"Error loading glossary: {e}")
    
    logger.info(f"Loaded {len(glossary)} terms from glossary")
    return glossary


def load_glossary_from_string(text: str) -> Glossary:
    """从字符串解析术语表（不依赖文件）。

    Args:
        text: 术语表文本内容（每行一个术语）

    Returns:
        Glossary instance
    """
    if not text or not text.strip():
        return Glossary()
    glossary = _parse_glossary_lines(text.strip().split('\n'))
    logger.info(f"Loaded {len(glossary)} terms from string")
    return glossary


def find_matching_terms(glossary: Glossary | Dict[str, str], text: str) -> Dict[str, str]:
    """
    Find glossary terms that appear in the given text.
    
    兼容旧的 Dict 接口和新的 Glossary 类。
    """
    if isinstance(glossary, Glossary):
        return glossary.find_matches(text)
    
    # 兼容旧的 Dict 接口
    text_lower = text.lower()
    return {
        term: trans
        for term, trans in glossary.items()
        if term.lower() in text_lower
    }
