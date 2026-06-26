"""Tests for SRT parser."""

import pytest
from pathlib import Path
import tempfile

from srt_translator.parser import parse_srt, save_srt, validate_srt_file
from srt_translator.models import SrtEntry


class TestParseSrt:
    
    def test_parse_simple(self):
        content = """1
00:00:01,000 --> 00:00:03,500
Hello world

2
00:00:04,000 --> 00:00:06,500
Goodbye world

"""
        entries = parse_srt(content)
        assert len(entries) == 2
        assert entries[0].text == "Hello world"
        assert entries[1].text == "Goodbye world"
    
    def test_parse_multiline(self):
        content = """1
00:00:01,000 --> 00:00:03,500
Line one
Line two

"""
        entries = parse_srt(content)
        assert entries[0].text == "Line one Line two"
    
    def test_parse_empty(self):
        assert parse_srt("") == []
        assert parse_srt("   \n\n  ") == []
    
    def test_parse_no_trailing_newline(self):
        """Test that last entry is captured even without trailing newlines."""
        content = """1
00:00:01,000 --> 00:00:03,500
First

2
00:00:04,000 --> 00:00:06,500
Last entry"""
        entries = parse_srt(content)
        assert len(entries) == 2
        assert entries[1].text == "Last entry"
    
    def test_parse_windows_line_endings(self):
        content = "1\r\n00:00:01,000 --> 00:00:03,500\r\nHello\r\n\r\n"
        entries = parse_srt(content)
        assert len(entries) == 1
        assert entries[0].text == "Hello"


class TestValidateSrtFile:
    
    def test_nonexistent(self):
        error = validate_srt_file(Path("/nonexistent/file.srt"))
        assert "not found" in error
    
    def test_wrong_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            error = validate_srt_file(Path(f.name))
            assert "Invalid file extension" in error
    
    def test_valid_file(self):
        with tempfile.NamedTemporaryFile(suffix=".srt", delete=False) as f:
            f.write(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")
            path = Path(f.name)
        
        try:
            error = validate_srt_file(path)
            assert error is None
        finally:
            path.unlink()

    def test_default_validation_does_not_reject_by_file_size(self, tmp_path):
        path = tmp_path / "large.srt"
        path.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n" + (" " * 64),
            encoding="utf-8",
        )

        assert validate_srt_file(path) is None
        assert "File too large" in validate_srt_file(path, max_size_bytes=16)


class TestSaveSrt:
    
    def test_save_and_reload(self):
        entries = [
            SrtEntry(1, "00:00:01,000", "00:00:03,500", "Hello"),
            SrtEntry(2, "00:00:04,000", "00:00:06,500", "World"),
        ]
        
        with tempfile.NamedTemporaryFile(suffix='.srt', delete=False) as f:
            path = Path(f.name)
        
        try:
            save_srt(entries, path)
            reloaded = parse_srt(path.read_text())
            
            assert len(reloaded) == 2
            assert reloaded[0].text == "Hello"
        finally:
            path.unlink()
