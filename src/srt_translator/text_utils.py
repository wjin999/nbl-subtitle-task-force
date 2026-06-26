"""Text processing utilities."""

from __future__ import annotations

import re


# 需要清理的标点（不包括中文常用标点）
REMOVABLE_PUNCTUATION = r'[#\-=+|\\<>]'

def clean_translated_text(text: str) -> str:
    """
    Clean and normalize translated subtitle text.
    
    只清理格式标记，保留正常标点符号。
    
    Args:
        text: Raw translated text
        
    Returns:
        Cleaned text
    """
    if not text or not isinstance(text, str):
        return ""
    
    text = text.strip()
    
    # 1. 移除开头的列表标记 (如 "1.", "2)", "-", "* ", "• ")
    #    注意：* 必须后接空格才被视为列表标记，避免误伤 markdown **bold**
    text = re.sub(r'^\s*(\d+[\.:\)]\s*|-\s+|\*\s+|•\s+)', '', text)
    
    # 2. 移除 markdown 格式标记（仅成对出现或首尾的）
    # 移除 **bold** (成对)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # 移除 __bold__ (成对)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # 移除 *italic* (成对，但避免误伤单个星号)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', text)
    # 移除 _italic_ (成对，但保留单词中的下划线)
    text = re.sub(r'(?<!\w)_(?!_)(.+?)(?<!\w)_(?!_)', r'\1', text)
    
    # 3. 移除多余的特殊字符，但保留正常标点
    text = re.sub(REMOVABLE_PUNCTUATION, ' ', text)
    
    # 3.5 移除中文句尾的句号（影视剧字幕规范：句末不加句号）
    # 但保留问号和叹号
    if re.search(r'[\u4e00-\u9fff]', text):
        text = re.sub(r'[。]+$', '', text)  # 移除末尾句号
        text = re.sub(r'([？！……])[。]+', r'\1', text)  # 问号/叹号/省略号后面的句号
    
    # 4. 标准化空格
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text


def validate_translation(original: str, translated: str) -> tuple[bool, str]:
    """
    Validate translation quality.
    
    Args:
        original: Original text
        translated: Translated text
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not translated:
        return False, "Empty translation"
    
    if translated.startswith("[Fail]"):
        return False, "Translation failed"
    
    # 检查是否全是乱码/特殊字符
    clean = re.sub(r'[\s\W]', '', translated)
    if not clean:
        return False, "Translation contains only special characters"
    
    # 检查长度异常（译文不应比原文长太多或短太多）
    orig_len = len(original)
    trans_len = len(translated)
    
    if orig_len > 10:  # 只对较长文本检查
        if trans_len < orig_len * 0.1:
            return False, f"Translation too short ({trans_len} vs {orig_len})"
        if trans_len > orig_len * 5:
            return False, f"Translation too long ({trans_len} vs {orig_len})"
    
    # 检查是否只是复制了原文
    if translated.strip().lower() == original.strip().lower():
        return False, "Translation is identical to original"
    
    return True, ""
