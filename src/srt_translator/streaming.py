"""Streaming subtitle block and spool utilities."""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .models import SrtEntry, SubtitleBlock
from .merger import init_spacy_model, should_merge
from .parser import iter_srt_entries

logger = logging.getLogger(__name__)


def append_ndjson(path: Path, payload: dict) -> None:
    """Append one JSON object to an NDJSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_block_spool(path: Path, blocks: Iterable[SubtitleBlock]) -> int:
    """Write subtitle blocks to an NDJSON spool file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for count, block in enumerate(blocks, 1):
            f.write(json.dumps(block.to_dict(), ensure_ascii=False) + "\n")
    logger.info("Wrote %s subtitle blocks to %s", count, path)
    return count


def append_block_spool(path: Path, block: SubtitleBlock) -> None:
    """Append one subtitle block to an NDJSON spool file."""
    append_ndjson(path, block.to_dict())


def iter_block_spool(path: Path) -> Iterator[SubtitleBlock]:
    """Iterate subtitle blocks from an NDJSON spool file."""
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield SubtitleBlock.from_dict(json.loads(line))
            except Exception as exc:
                logger.warning("Skipping invalid spool line %s in %s: %s", line_num, path, exc)


def recover_block_spool(path: Path, stop_before_failed: bool = False) -> int:
    """Keep the valid contiguous prefix of a possibly interrupted block spool."""
    if not path.exists():
        return 0

    repaired_path = path.with_suffix(path.suffix + ".repair")
    count = 0
    with path.open("r", encoding="utf-8") as source, repaired_path.open(
        "w", encoding="utf-8"
    ) as target:
        for line in source:
            if not line.strip():
                continue
            try:
                block = SubtitleBlock.from_dict(json.loads(line))
            except Exception:
                break
            if block.block_id != count:
                break
            if stop_before_failed and block.stage == "failed":
                break
            target.write(json.dumps(block.to_dict(), ensure_ascii=False) + "\n")
            count += 1
    repaired_path.replace(path)
    return count


def count_spool_lines(path: Path) -> int:
    """Count non-empty NDJSON rows."""
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _new_block(block_id: int, entry: SrtEntry) -> SubtitleBlock:
    return SubtitleBlock(
        block_id=block_id,
        entry=entry.copy(index=block_id + 1),
        source_entries=[entry.copy()],
        source_start_index=entry.index,
        source_end_index=entry.index,
    )


def _merge_block_with_entry(block: SubtitleBlock, entry: SrtEntry) -> SubtitleBlock:
    merged_entry = block.entry.copy(
        text=f"{block.entry.text} {entry.text}",
        end=entry.end,
    )
    return block.copy(
        entry=merged_entry,
        source_entries=[*block.source_entries, entry.copy()],
        source_end_index=entry.index,
    )


def iter_merged_blocks(
    entries: Iterable[SrtEntry],
    max_chars: int,
    time_gap_threshold: float,
    max_duration_seconds: float | None = 15.0,
) -> Iterator[SubtitleBlock]:
    """Stream smart-merged English subtitle blocks."""
    init_spacy_model()
    current: SubtitleBlock | None = None
    previous_source: SrtEntry | None = None
    block_id = 0

    for entry in entries:
        if current is None:
            current = _new_block(block_id, entry)
            previous_source = entry
            block_id += 1
            continue

        assert previous_source is not None
        combined_len = len(current.entry.text) + 1 + len(entry.text)
        combined_duration = entry.end_seconds - current.entry.start_seconds
        within_duration = (
            max_duration_seconds is None
            or combined_duration <= max_duration_seconds
        )
        if (
            should_merge(previous_source, entry, max_chars, time_gap_threshold)
            and combined_len <= max_chars
            and within_duration
        ):
            current = _merge_block_with_entry(current, entry)
        else:
            yield current
            current = _new_block(block_id, entry)
            block_id += 1

        previous_source = entry

    if current is not None:
        yield current


def build_block_spool(
    input_path: Path,
    spool_path: Path,
    max_chars: int,
    time_gap_threshold: float,
) -> int:
    """Stream-read an SRT file, smart-merge entries, and write block spool."""
    temporary_path = spool_path.with_suffix(spool_path.suffix + ".tmp")
    count = write_block_spool(
        temporary_path,
        iter_merged_blocks(
            iter_srt_entries(input_path),
            max_chars=max_chars,
            time_gap_threshold=time_gap_threshold,
        ),
    )
    temporary_path.replace(spool_path)
    return count


def iter_block_windows(
    spool_path: Path,
    max_entries: int,
    max_chars: int,
) -> Iterator[list[SubtitleBlock]]:
    """Yield analysis windows from a block spool."""
    window: list[SubtitleBlock] = []
    char_count = 0
    for block in iter_block_spool(spool_path):
        block_len = len(block.entry.text)
        if window and (len(window) >= max_entries or char_count + block_len > max_chars):
            yield window
            window = []
            char_count = 0
        window.append(block)
        char_count += block_len
    if window:
        yield window


def iter_chunks_with_context(
    spool_path: Path,
    chunk_size: int,
    context_window: int,
) -> Iterator[tuple[list[SubtitleBlock], list[str], list[str]]]:
    """Yield sequential chunks with previous and next context text."""
    block_iter = iter_block_spool(spool_path)
    previous_texts: deque[str] = deque(maxlen=context_window)
    buffer: deque[SubtitleBlock] = deque()

    def fill_buffer() -> None:
        target = chunk_size + context_window
        while len(buffer) < target:
            try:
                buffer.append(next(block_iter))
            except StopIteration:
                break

    fill_buffer()
    while buffer:
        chunk_len = min(chunk_size, len(buffer))
        chunk = [buffer[i] for i in range(chunk_len)]
        next_context = [
            buffer[i].entry.text
            for i in range(chunk_len, min(len(buffer), chunk_len + context_window))
        ]
        yield chunk, list(previous_texts), next_context

        for _ in range(chunk_len):
            done = buffer.popleft()
            previous_texts.append(done.entry.text)
        fill_buffer()


def save_blocks_as_srt(blocks: Iterable[SubtitleBlock], path: Path) -> int:
    """Write subtitle blocks as an SRT file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary_path.open("w", encoding="utf-8") as f:
        for count, block in enumerate(blocks, 1):
            f.write(block.entry.to_srt(count))
    temporary_path.replace(path)
    logger.info("Saved %s subtitle blocks to %s", count, path)
    return count


def block_texts(blocks: Sequence[SubtitleBlock]) -> str:
    """Return compact text for a block sequence."""
    return "\n".join(f"{block.block_id}: {block.entry.text}" for block in blocks)
