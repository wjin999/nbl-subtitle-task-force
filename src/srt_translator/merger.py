"""Intelligent subtitle merging using spaCy NLP."""

from __future__ import annotations

import asyncio
import logging
import sys
import os
import importlib
from concurrent.futures import ThreadPoolExecutor
from typing import List, Sequence, Optional, TYPE_CHECKING

from .models import SrtEntry

if TYPE_CHECKING:
    import spacy

logger = logging.getLogger(__name__)

# Global English NLP model instance
_nlp_model: Optional["spacy.Language"] = None
_merge_executor = ThreadPoolExecutor(max_workers=1)

ENGLISH_SPACY_MODEL = "en_core_web_sm"


def _resolve_spacy_model_path(model_package: str) -> str | None:
    """Resolve a spaCy model data path for PyInstaller bundles.

    In a PyInstaller bundle, importing the model package imports the frozen
    module from the PYZ archive -- its __file__ is not a real filesystem
    path. The actual model data files are extracted to sys._MEIPASS via
    the 'datas' directive in the spec file. We must check MEIPASS first.
    """
    frozen = getattr(sys, 'frozen', False)

    # 1. PyInstaller: look in MEIPASS where 'datas' are extracted
    if frozen:
        bundle = sys._MEIPASS  # type: ignore[attr-defined]
        pkg_dir = os.path.join(bundle, model_package)
        if not os.path.isdir(pkg_dir):
            return None
    else:
        # 2. Normal pip-installed environment
        try:
            model_module = importlib.import_module(model_package)
            pkg_dir = os.path.dirname(model_module.__file__)
        except ImportError:
            return None

    # Find the model data subdirectory (named <model_name>-<version>)
    for entry in os.listdir(pkg_dir):
        entry_path = os.path.join(pkg_dir, entry)
        if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, 'config.cfg')):
            return entry_path

    return None


def init_spacy_model() -> None:
    """
    Initialize the English spaCy NLP model.

    Supports:
    - pip-installed model (normal environment)
    - PyInstaller bundled model via datas (frozen environment)
    """
    global _nlp_model

    model_package = ENGLISH_SPACY_MODEL
    if _nlp_model is not None:
        return

    try:
        import spacy
        # Force CPU mode -- GPU detection can hang in PyInstaller bundles
        try:
            spacy.prefer_gpu(False)
        except Exception:
            pass
    except ImportError:
        raise ImportError(
            "spaCy is required. Install it with 'pip install spacy'."
        )

    logger.info(f"Initializing English spaCy NLP model: {model_package}...")

    model_path = _resolve_spacy_model_path(model_package)
    if model_path:
        logger.debug(f"Loading spaCy model from: {model_path}")
        try:
            _nlp_model = spacy.load(
                model_path,
                disable=["ner", "lemmatizer"]
            )
            logger.info("spaCy model loaded from bundled path")
            return
        except Exception as e:
            logger.error(f"Failed to load spaCy model from path: {model_path}: {e}")
            raise OSError(
                f"spaCy model data found at '{model_path}' but failed to load: {e}\n"
                "The model files may be incomplete. Rebuild with: pyinstaller api-server.spec -y"
            )

    # Not frozen and no model path -- try standard pip-installed load
    if not getattr(sys, 'frozen', False):
        try:
            _nlp_model = spacy.load(
                model_package,
                disable=["ner", "lemmatizer"]
            )
            logger.info("spaCy model loaded from pip install")
            return
        except OSError:
            pass

    raise OSError(
        f"spaCy model '{model_package}' not found.\n"
        f"Install: python -m spacy download {model_package}"
    )


def _check_sentence_boundary(text1: str, text2: str) -> bool:
    """
    Check if combining two texts would cross a sentence boundary.
    
    Returns:
        True if they should be merged (no boundary between them)
    """
    global _nlp_model
    
    if _nlp_model is None:
        raise RuntimeError("spaCy model not initialized")
    
    combined = f"{text1} {text2}"
    doc = _nlp_model(combined)
    sentences = list(doc.sents)
    
    # 如果只有一个句子，可以合并
    if len(sentences) == 1:
        return True
    
    # 检查句子边界是否在拼接点附近
    split_point = len(text1) + 1  # +1 for space
    
    for sent in sentences:
        # 如果句子边界在拼接点附近（±2字符），不应合并
        if abs(sent.start_char - split_point) <= 2:
            return False
    
    return True


def should_merge(
    cur_entry: SrtEntry, 
    next_entry: SrtEntry, 
    max_chars: int,
    time_gap_threshold: float,
) -> bool:
    """
    Determine if two subtitle entries should be merged.
    """
    global _nlp_model

    cur_text = cur_entry.text
    next_text = next_entry.text
    
    # 基础检查
    if not cur_text or not next_text:
        return False
    
    # 长度检查
    if len(cur_text) + 1 + len(next_text) > max_chars:
        return False
    
    # 时间间隔检查
    time_gap = next_entry.start_seconds - cur_entry.end_seconds
    if time_gap > time_gap_threshold:
        return False
    
    # 如果当前字幕以句号等结尾，不合并
    if cur_text.rstrip()[-1:] in '.!?。！？':
        return False

    if _nlp_model is None:
        init_spacy_model()

    # NLP 句子边界检测
    return _check_sentence_boundary(cur_text, next_text)


def _apply_merge_decisions(
    entries: Sequence[SrtEntry],
    decisions: List[bool],
    max_chars: int,
    max_duration_seconds: float | None,
) -> List[SrtEntry]:
    """Apply pre-computed merge decisions to produce merged entry list.

    Pairwise merge decisions can form long chains (A+B, B+C, C+D). Re-check
    cumulative limits while applying decisions so a translated cue never grows
    far beyond the configured per-entry bounds.
    """
    merged: List[SrtEntry] = []
    current = entries[0].copy()

    for i in range(1, len(entries)):
        next_entry = entries[i]
        combined_len = len(current.text) + 1 + len(next_entry.text)
        combined_duration = next_entry.end_seconds - current.start_seconds
        within_duration = (
            max_duration_seconds is None
            or combined_duration <= max_duration_seconds
        )

        if decisions[i - 1] and combined_len <= max_chars and within_duration:
            current = current.copy(
                text=current.text + " " + next_entry.text,
                end=next_entry.end,
            )
        else:
            merged.append(current)
            current = next_entry.copy()

    merged.append(current)
    return merged


def merge_entries(
    entries: Sequence[SrtEntry],
    max_chars: int = 300,
    time_gap_threshold: float = 1.5,
    max_duration_seconds: float | None = 15.0,
) -> List[SrtEntry]:
    """
    Merge subtitle entries using intelligent NLP-based logic.

    预计算所有相邻对的合并决策，避免在合并过程中因 current 文本变化
    导致 should_merge 中对合并后文本重复 spaCy 解析。

    注意：此函数不会修改原始 entries，而是返回新的列表。
    """
    if not entries:
        return []
    if len(entries) == 1:
        return [entries[0].copy()]

    decisions = [
        should_merge(
            entries[i],
            entries[i + 1],
            max_chars,
            time_gap_threshold,
        )
        for i in range(len(entries) - 1)
    ]

    merged = _apply_merge_decisions(
        entries,
        decisions,
        max_chars,
        max_duration_seconds,
    )
    logger.info(f"Merged {len(entries)} entries into {len(merged)} entries")
    return merged


def merge_entries_batch(
    entries: Sequence[SrtEntry],
    max_chars: int = 300,
    time_gap_threshold: float = 1.5,
    batch_size: int = 100,
    max_duration_seconds: float | None = 15.0,
) -> List[SrtEntry]:
    """
    Batch-optimized version of merge_entries.
    
    使用 spaCy 的 nlp.pipe() 批量处理以提高性能。
    适用于大量字幕条目。
    """
    global _nlp_model
    
    if not entries:
        return []
    
    if _nlp_model is None:
        init_spacy_model()
    
    # 对于小数据集，使用普通方法
    if len(entries) < batch_size:
        return merge_entries(
            entries,
            max_chars,
            time_gap_threshold,
            max_duration_seconds,
        )
    
    # 预计算所有相邻对的合并文本
    pairs_to_check: List[tuple[int, str]] = []
    
    for i in range(len(entries) - 1):
        cur = entries[i]
        nxt = entries[i + 1]
        
        # 先做快速检查
        if not cur.text or not nxt.text:
            continue
        if len(cur.text) + 1 + len(nxt.text) > max_chars:
            continue
        if nxt.start_seconds - cur.end_seconds > time_gap_threshold:
            continue
        if cur.text.rstrip()[-1:] in '.!?。！？':
            continue
        
        # 需要 NLP 检查的对
        combined = f"{cur.text} {nxt.text}"
        pairs_to_check.append((i, combined))
    
    # 批量 NLP 处理
    should_merge_set: set[int] = set()
    
    if pairs_to_check:
        texts = [p[1] for p in pairs_to_check]
        docs = list(_nlp_model.pipe(texts, batch_size=batch_size))
        
        for (idx, _), doc in zip(pairs_to_check, docs):
            sentences = list(doc.sents)
            if len(sentences) == 1:
                should_merge_set.add(idx)
            else:
                # 检查边界
                split_point = len(entries[idx].text) + 1
                merge_ok = True
                for sent in sentences:
                    if abs(sent.start_char - split_point) <= 2:
                        merge_ok = False
                        break
                if merge_ok:
                    should_merge_set.add(idx)
    
    # 执行合并
    decisions = [(i in should_merge_set) for i in range(len(entries) - 1)]
    merged = _apply_merge_decisions(
        entries,
        decisions,
        max_chars,
        max_duration_seconds,
    )

    logger.info(f"Batch merged {len(entries)} entries into {len(merged)} entries")
    return merged


async def init_spacy_model_async() -> None:
    """Async-safe version of init_spacy_model.

    Runs spaCy model loading in a thread executor so the asyncio event
    loop is not blocked. This allows CancelledError to propagate even
    while the model is loading (PyInstaller bundles can take 10-30s).
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_merge_executor, init_spacy_model)


async def merge_entries_batch_async(
    entries: Sequence[SrtEntry],
    max_chars: int = 300,
    time_gap_threshold: float = 1.5,
    batch_size: int = 100,
    max_duration_seconds: float | None = 15.0,
) -> List[SrtEntry]:
    """Async-safe version of merge_entries_batch."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _merge_executor,
        merge_entries_batch,
        entries,
        max_chars,
        time_gap_threshold,
        batch_size,
        max_duration_seconds,
    )
