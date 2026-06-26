"""Tests for CLI argument parsing and main flow."""
from __future__ import annotations

import sys
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
import argparse

from srt_translator.cli import parse_arguments, main_async


class TestParseArguments:
    """Test CLI argument parsing."""

    def test_minimal_args(self):
        """Test parsing with only input path."""
        test_args = ["nbl-subtitle-task-force", "input.srt"]
        with patch.object(sys, "argv", test_args):
            args = parse_arguments()
        assert args.input_path == "input.srt"
        assert args.output_path is None
        assert args.glossary_path is None
        assert args.max_chars_per_entry == 300
        assert args.save_merged is False
        assert args.merged_output_path is None
        assert args.api_key is None
        assert args.model_name is None
        assert args.verbose is False

    def test_all_args(self):
        """Test parsing with all arguments."""
        test_args = [
            "nbl-subtitle-task-force", "input.srt", "output.srt",
            "-g", "glossary.txt",
            "--save-merged",
            "--merged-output", "merged.srt",
            "--max-chars", "200",
            "--merge-gap", "2.0",
            "--api-key", "test-key",
            "--model", "custom-model",
            "--summary-model", "custom-summary-model",
            "--chunk-size", "5",
            "--resume",
            "-v",
        ]
        with patch.object(sys, "argv", test_args):
            args = parse_arguments()
        assert args.input_path == "input.srt"
        assert args.output_path == "output.srt"
        assert args.glossary_path == "glossary.txt"
        assert args.max_chars_per_entry == 200
        assert args.save_merged is True
        assert args.merged_output_path == "merged.srt"
        assert args.merge_time_gap == 2.0
        assert args.api_key == "test-key"
        assert args.model_name == "custom-model"
        assert args.summary_model_name == "custom-summary-model"
        assert args.chunk_size_for_translation == 5
        assert args.resume is True
        assert args.verbose is True

    def test_default_output_path(self):
        """Test that output path defaults to None when not specified."""
        test_args = ["nbl-subtitle-task-force", "input.srt"]
        with patch.object(sys, "argv", test_args):
            args = parse_arguments()
        assert args.output_path is None

    def test_output_option(self):
        """Test specifying output with -o/--output."""
        test_args = ["nbl-subtitle-task-force", "input.srt", "-o", "output.srt"]
        with patch.object(sys, "argv", test_args):
            args = parse_arguments()
        assert args.output_path == "output.srt"

    def test_rejects_duplicate_output_paths(self):
        """Test that positional output and -o cannot both be used."""
        test_args = ["nbl-subtitle-task-force", "input.srt", "positional.srt", "-o", "option.srt"]
        with patch.object(sys, "argv", test_args), pytest.raises(SystemExit):
            parse_arguments()


class TestMainAsync:
    """Test the async main workflow."""

    @pytest.fixture
    def mock_args(self):
        """Create mock args for testing."""
        args = MagicMock(spec=argparse.Namespace)
        args.input_path = str(Path("tests/test_input.srt"))
        args.output_path = None
        args.glossary_path = None
        args.api_key = "test-key"
        args.max_chars_per_entry = 300
        args.merge_time_gap = 1.5
        args.api_key = "test-key"
        args.model_name = "test-model"
        args.summary_model_name = "test-model"
        args.chunk_size_for_translation = 10
        args.resume = False
        args.save_merged = False
        args.merged_output_path = None
        args.verbose = False
        args.summary_prompt = None
        args.translation_prompt = None
        args.max_output_tokens = 4096
        args.request_timeout = 60.0
        args.context_window = 7
        return args

    @pytest.mark.asyncio
    async def test_main_async_no_input_file(self, mock_args, tmp_path):
        """Test main_async when input file doesn't exist."""
        mock_args.input_path = str(tmp_path / "nonexistent.srt")

        result = await main_async(mock_args)
        assert result == 1  # Should return error code

    @pytest.mark.asyncio
    async def test_main_async_invalid_srt(self, mock_args, tmp_path):
        """Test main_async with invalid SRT content."""
        srt_path = tmp_path / "test.srt"
        srt_path.write_text("Not a valid SRT file", encoding="utf-8")

        mock_args.input_path = str(srt_path)
        result = await main_async(mock_args)
        assert result == 1

    @pytest.mark.asyncio
    async def test_main_async_valid_srt(self, mock_args, tmp_path):
        """Test main_async with valid SRT content and mocked streaming pipeline."""
        srt_path = tmp_path / "test.srt"
        srt_path.write_text(
            "1\n00:00:01,000 --> 00:00:03,500\nHello world\n\n"
            "2\n00:00:04,000 --> 00:00:06,500\nHow are you?\n",
            encoding="utf-8-sig"
        )
        mock_args.input_path = str(srt_path)

        with patch("srt_translator.cli.create_client") as mock_create_client, \
             patch("srt_translator.cli.StreamingAgentPipeline.run_file", new_callable=AsyncMock) as mock_run_file:
            from srt_translator.streaming_pipeline import StreamingAgentResult

            mock_client = MagicMock()
            mock_create_client.return_value = mock_client
            output_path = srt_path.with_name(f"translated_{srt_path.name}")
            report_path = output_path.with_name(f"{output_path.stem}.agent-report.json")
            mock_run_file.return_value = StreamingAgentResult(
                output_path=output_path,
                report_path=report_path,
                block_count=2,
                translated_count=2,
            )

            result = await main_async(mock_args)
            assert result == 0  # Success
            mock_run_file.assert_awaited_once()
            assert mock_run_file.await_args.kwargs["input_path"] == srt_path
            assert mock_run_file.await_args.kwargs["output_path"] == output_path
            assert mock_run_file.await_args.kwargs["report_path"] == report_path

    @pytest.mark.asyncio
    async def test_main_async_can_save_merged_output(self, mock_args, tmp_path):
        """CLI passes merged output path into the streaming pipeline."""
        srt_path = tmp_path / "test.srt"
        srt_path.write_text(
            "1\n00:00:01,000 --> 00:00:03,500\nHello world\n",
            encoding="utf-8-sig"
        )
        mock_args.input_path = str(srt_path)
        mock_args.save_merged = True

        with patch("srt_translator.cli.create_client") as mock_create_client, \
             patch("srt_translator.cli.StreamingAgentPipeline.run_file", new_callable=AsyncMock) as mock_run_file:
            from srt_translator.streaming_pipeline import StreamingAgentResult

            mock_create_client.return_value = MagicMock()
            output_path = srt_path.with_name(f"translated_{srt_path.name}")
            report_path = output_path.with_name(f"{output_path.stem}.agent-report.json")
            mock_run_file.return_value = StreamingAgentResult(
                output_path=output_path,
                report_path=report_path,
                block_count=1,
                translated_count=1,
            )

            result = await main_async(mock_args)

            assert result == 0
            assert mock_run_file.await_args.kwargs["merged_output_path"] == srt_path.with_name(
                f"merged_{srt_path.name}"
            )
