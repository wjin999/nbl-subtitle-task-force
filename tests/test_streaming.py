"""Tests for streaming subtitle utilities."""

from __future__ import annotations

from srt_translator.models import SrtEntry
from srt_translator.parser import iter_srt_entries
from srt_translator.streaming import (
    append_block_spool,
    iter_block_spool,
    iter_merged_blocks,
    recover_block_spool,
    write_block_spool,
)
from srt_translator.models import SubtitleBlock


def test_iter_srt_entries_handles_crlf(tmp_path):
    srt_path = tmp_path / "sample.srt"
    srt_path.write_bytes(
        b"1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n\r\n"
        b"2\r\n00:00:02,100 --> 00:00:03,000\r\nworld\r\n",
    )

    entries = list(iter_srt_entries(srt_path))

    assert [entry.text for entry in entries] == ["Hello", "world"]
    assert entries[0].timecode == "00:00:01,000 --> 00:00:02,000"


def test_streaming_merge_respects_cumulative_limits(monkeypatch):
    monkeypatch.setattr("srt_translator.streaming.init_spacy_model", lambda: None)
    monkeypatch.setattr("srt_translator.streaming.should_merge", lambda *args: True)
    entries = [
        SrtEntry(1, "00:00:01,000", "00:00:02,000", "aaaaa"),
        SrtEntry(2, "00:00:02,100", "00:00:03,000", "bbbbb"),
        SrtEntry(3, "00:00:03,100", "00:00:04,000", "ccccc"),
    ]

    blocks = list(iter_merged_blocks(entries, max_chars=11, time_gap_threshold=1.5))

    assert [block.entry.text for block in blocks] == ["aaaaa bbbbb", "ccccc"]
    assert blocks[0].source_start_index == 1
    assert blocks[0].source_end_index == 2


def test_block_spool_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("srt_translator.streaming.init_spacy_model", lambda: None)
    monkeypatch.setattr("srt_translator.streaming.should_merge", lambda *args: False)
    entries = [
        SrtEntry(1, "00:00:01,000", "00:00:02,000", "Hello"),
        SrtEntry(2, "00:00:02,100", "00:00:03,000", "world"),
    ]
    spool_path = tmp_path / "blocks.ndjson"

    count = write_block_spool(
        spool_path,
        iter_merged_blocks(entries, max_chars=300, time_gap_threshold=1.5),
    )
    blocks = list(iter_block_spool(spool_path))

    assert count == 2
    assert [block.entry.text for block in blocks] == ["Hello", "world"]


def test_recover_block_spool_truncates_invalid_tail_and_failed_resume(tmp_path):
    path = tmp_path / "translated.ndjson"
    first = SrtEntry(1, "00:00:01,000", "00:00:02,000", "你好")
    failed = SrtEntry(2, "00:00:02,100", "00:00:03,000", "world")
    append_block_spool(
        path,
        SubtitleBlock(0, first, [first], 1, 1, stage="translated"),
    )
    append_block_spool(
        path,
        SubtitleBlock(1, failed, [failed], 2, 2, stage="failed"),
    )
    with path.open("a", encoding="utf-8") as target:
        target.write('{"broken":')

    count = recover_block_spool(path, stop_before_failed=True)

    assert count == 1
    assert [block.block_id for block in iter_block_spool(path)] == [0]
