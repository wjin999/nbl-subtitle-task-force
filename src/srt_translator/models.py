"""Data models for SRT subtitle entries."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SrtEntry:
    """Represents a single subtitle entry in SRT format."""
    
    index: int
    start: str
    end: str
    text: str
    
    # 缓存时间戳解析结果
    _start_cache: Optional[float] = field(default=None, repr=False, compare=False)
    _end_cache: Optional[float] = field(default=None, repr=False, compare=False)

    @property
    def timecode(self) -> str:
        """Return the timecode line in SRT format."""
        return f"{self.start} --> {self.end}"
    
    @property
    def start_seconds(self) -> float:
        """Convert start timecode to seconds (cached)."""
        if self._start_cache is None:
            self._start_cache = self._time_str_to_seconds(self.start)
        return self._start_cache

    @property
    def end_seconds(self) -> float:
        """Convert end timecode to seconds (cached)."""
        if self._end_cache is None:
            self._end_cache = self._time_str_to_seconds(self.end)
        return self._end_cache

    @staticmethod
    def _time_str_to_seconds(t_str: str) -> float:
        """Convert SRT timecode string to seconds."""
        try:
            h, m, s_full = t_str.split(':')
            s, ms = s_full.split(',')
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
        except (ValueError, AttributeError):
            return 0.0

    def to_srt(self, new_idx: int | None = None) -> str:
        """Convert entry to SRT format string."""
        idx = new_idx if new_idx is not None else self.index
        return f"{idx}\n{self.timecode}\n{self.text}\n\n"
    
    def copy(self, **changes) -> "SrtEntry":
        """Create a copy with optional field changes."""
        new = SrtEntry(
            index=changes.get('index', self.index),
            start=changes.get('start', self.start),
            end=changes.get('end', self.end),
            text=changes.get('text', self.text),
        )
        # 携带缓存的解析值
        if 'start' not in changes and self._start_cache is not None:
            new._start_cache = self._start_cache
        if 'end' not in changes and self._end_cache is not None:
            new._end_cache = self._end_cache
        return new


@dataclass
class SubtitleBlock:
    """A merged subtitle block with provenance for streaming agent stages."""

    block_id: int
    entry: SrtEntry
    source_entries: list[SrtEntry]
    source_start_index: int
    source_end_index: int
    stage: str = "pending"

    def copy(self, **changes) -> "SubtitleBlock":
        """Create a copy with optional field changes."""
        return SubtitleBlock(
            block_id=changes.get("block_id", self.block_id),
            entry=changes.get("entry", self.entry.copy()),
            source_entries=changes.get(
                "source_entries",
                [entry.copy() for entry in self.source_entries],
            ),
            source_start_index=changes.get("source_start_index", self.source_start_index),
            source_end_index=changes.get("source_end_index", self.source_end_index),
            stage=changes.get("stage", self.stage),
        )

    @staticmethod
    def _entry_to_dict(entry: SrtEntry) -> dict:
        return {
            "index": entry.index,
            "start": entry.start,
            "end": entry.end,
            "text": entry.text,
        }

    @staticmethod
    def _entry_from_dict(data: dict) -> SrtEntry:
        return SrtEntry(
            int(data["index"]),
            str(data["start"]),
            str(data["end"]),
            str(data.get("text", "")),
        )

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "block_id": self.block_id,
            "entry": self._entry_to_dict(self.entry),
            "source_entries": [
                self._entry_to_dict(entry) for entry in self.source_entries
            ],
            "source_start_index": self.source_start_index,
            "source_end_index": self.source_end_index,
            "stage": self.stage,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubtitleBlock":
        """Deserialize from a JSON-compatible dictionary."""
        return cls(
            block_id=int(data["block_id"]),
            entry=cls._entry_from_dict(data["entry"]),
            source_entries=[
                cls._entry_from_dict(entry) for entry in data.get("source_entries", [])
            ],
            source_start_index=int(data["source_start_index"]),
            source_end_index=int(data["source_end_index"]),
            stage=str(data.get("stage", "pending")),
        )
