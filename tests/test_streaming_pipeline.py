"""Tests for the streaming agent pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srt_translator.agent_plan import AgentPlan, AgentReport, AgentWindowAnalysis
from srt_translator.config import TranslatorConfig
from srt_translator.glossary import Glossary
from srt_translator.llm_client import LLMCallError
from srt_translator.models import SrtEntry, SubtitleBlock
from srt_translator.streaming import append_block_spool, iter_block_spool, write_block_spool
from srt_translator.streaming_pipeline import (
    ResumeMismatchError,
    StreamingAgentPipeline,
)
from srt_translator.translator import TranslationResult, _review_chunk_results


def _block(block_id: int, text: str = "Hello") -> SubtitleBlock:
    entry = SrtEntry(block_id + 1, "00:00:01,000", "00:00:02,000", text)
    return SubtitleBlock(
        block_id=block_id,
        entry=entry,
        source_entries=[entry.copy()],
        source_start_index=entry.index,
        source_end_index=entry.index,
    )


@pytest.mark.asyncio
async def test_streaming_pipeline_writes_srt_and_report(tmp_path):
    input_path = tmp_path / "input.srt"
    input_path.write_text("placeholder", encoding="utf-8")
    output_path = tmp_path / "translated_input.srt"
    report_path = tmp_path / "translated_input.agent-report.json"
    spool_dir = tmp_path / "spool"
    config = TranslatorConfig(
        api_key="test-key",
        model_name="test-model",
        summary_model_name="test-model",
        chunk_size=2,
    )

    def fake_build(_input_path, spool_path, *_args):
        append_block_spool(spool_path, _block(0, "Hello"))
        append_block_spool(spool_path, _block(1, "world"))
        return 2

    async def fake_translate(**kwargs):
        return [
            TranslationResult(item["index"], item["text"], f"译文{item['index']}", True)
            for item in kwargs["chunk_data"]
        ]

    with patch("srt_translator.streaming_pipeline.build_block_spool", side_effect=fake_build), \
         patch("srt_translator.streaming_pipeline.analyze_window", new_callable=AsyncMock) as mock_analyze, \
         patch("srt_translator.streaming_pipeline.reduce_agent_plan", new_callable=AsyncMock) as mock_reduce, \
         patch("srt_translator.streaming_pipeline.translate_chunk_task", new_callable=AsyncMock) as mock_translate, \
         patch("srt_translator.streaming_pipeline.call_llm_async", new_callable=AsyncMock) as mock_call:

        mock_analyze.return_value = AgentWindowAnalysis(0, 0, 1, summary="test")
        mock_reduce.return_value = AgentPlan(style_guide="口语化")
        mock_translate.side_effect = fake_translate
        mock_call.return_value = '{"corrections":[]}'

        result = await StreamingAgentPipeline(config).run_file(
            input_path=input_path,
            output_path=output_path,
            report_path=report_path,
            glossary=Glossary(),
            client=MagicMock(),
            spool_dir=spool_dir,
        )

    assert result.block_count == 2
    assert output_path.read_text(encoding="utf-8").count("-->") == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["stats"]["block_count"] == 2
    assert report["agent_plan"]["style_guide"] == "口语化"


@pytest.mark.asyncio
async def test_translate_with_repair_splits_failed_chunk():
    config = TranslatorConfig(
        api_key="test-key",
        model_name="test-model",
        summary_model_name="test-model",
        chunk_size=2,
        repair_attempts=3,
    )
    pipeline = StreamingAgentPipeline(config)
    report = AgentReport("in.srt", "out.srt")
    calls: list[int] = []

    async def fake_translate(**kwargs):
        size = len(kwargs["chunk_data"])
        calls.append(size)
        if size > 1:
            raise RuntimeError("chunk too large")
        item = kwargs["chunk_data"][0]
        return [TranslationResult(item["index"], item["text"], "你好", True)]

    with patch("srt_translator.streaming_pipeline.translate_chunk_task", new_callable=AsyncMock) as mock_translate:
        mock_translate.side_effect = fake_translate
        blocks = await pipeline._translate_with_repair(
            [_block(0), _block(1, "world")],
            [],
            [],
            AgentPlan(style_guide="口语化"),
            MagicMock(),
            None,
            report,
            attempts_left=3,
        )

    assert calls == [2, 1, 1]
    assert [block.entry.text for block in blocks] == ["你好", "你好"]
    assert report.retries


@pytest.mark.asyncio
async def test_translate_with_repair_propagates_system_llm_failure():
    pipeline = StreamingAgentPipeline(
        TranslatorConfig(api_key="test-key", model_name="m", summary_model_name="m")
    )
    report = AgentReport("in.srt", "out.srt")

    with patch(
        "srt_translator.streaming_pipeline.translate_chunk_task",
        new_callable=AsyncMock,
        side_effect=LLMCallError("authentication failed"),
    ):
        with pytest.raises(LLMCallError):
            await pipeline._translate_with_repair(
                [_block(0)],
                [],
                [],
                AgentPlan(style_guide="口语化"),
                MagicMock(),
                None,
                report,
                attempts_left=3,
            )

    assert report.failures == []
    assert report.stats["failed_request_count"] == 1


@pytest.mark.asyncio
async def test_inner_review_does_not_hide_system_llm_failure():
    draft = [TranslationResult(0, "Hello", "你好", True)]

    with patch(
        "srt_translator.translator.call_llm_async",
        new_callable=AsyncMock,
        side_effect=LLMCallError("service unavailable"),
    ):
        with pytest.raises(LLMCallError):
            await _review_chunk_results(
                MagicMock(),
                "m",
                [{"index": 0, "text": "Hello"}],
                draft,
                [],
                [],
                "brief",
                [],
                1024,
            )


@pytest.mark.asyncio
async def test_pipeline_reports_completed_with_warnings_for_local_fallback(tmp_path):
    input_path = tmp_path / "input.srt"
    input_path.write_text("placeholder", encoding="utf-8")
    output_path = tmp_path / "translated_input.srt"
    report_path = tmp_path / "translated_input.agent-report.json"
    config = TranslatorConfig(
        api_key="test-key",
        model_name="test-model",
        summary_model_name="test-model",
        chunk_size=2,
        minimum_success_ratio=0.5,
    )

    def fake_build(_input_path, spool_path, *_args):
        append_block_spool(spool_path, _block(0, "Hello"))
        append_block_spool(spool_path, _block(1, "world"))
        return 2

    async def fake_translate(**kwargs):
        return [
            TranslationResult(item["index"], item["text"], "你好", item["index"] == 0)
            for item in kwargs["chunk_data"]
        ]

    with patch("srt_translator.streaming_pipeline.build_block_spool", side_effect=fake_build), \
         patch("srt_translator.streaming_pipeline.analyze_window", new_callable=AsyncMock) as analyze, \
         patch("srt_translator.streaming_pipeline.reduce_agent_plan", new_callable=AsyncMock) as reduce, \
         patch("srt_translator.streaming_pipeline.translate_chunk_task", new_callable=AsyncMock) as translate, \
         patch("srt_translator.streaming_pipeline.call_llm_async", new_callable=AsyncMock) as call:
        analyze.return_value = AgentWindowAnalysis(0, 0, 1, summary="test")
        reduce.return_value = AgentPlan(style_guide="口语化")
        translate.side_effect = fake_translate
        call.return_value = '{"corrections":[]}'
        result = await StreamingAgentPipeline(config).run_file(
            input_path,
            output_path,
            report_path,
            Glossary(),
            MagicMock(),
            spool_dir=tmp_path / "spool",
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.status == "completed_with_warnings"
    assert result.translated_count == 1
    assert result.fallback_original_count == 1
    assert report["status"] == "completed_with_warnings"
    assert report["stats"]["fallback_original_count"] == 1


def test_resume_manifest_rejects_changed_input(tmp_path):
    input_path = tmp_path / "input.srt"
    input_path.write_text("original", encoding="utf-8")
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    pipeline = StreamingAgentPipeline(
        TranslatorConfig(api_key="test-key", model_name="m", summary_model_name="m")
    )
    paths = pipeline._spool_paths(spool_dir)

    pipeline._prepare_run(paths, input_path, Glossary(), None, None, resume=False)
    input_path.write_text("changed", encoding="utf-8")

    with pytest.raises(ResumeMismatchError):
        pipeline._prepare_run(paths, input_path, Glossary(), None, None, resume=True)


def test_report_details_are_spooled_when_failure_sample_is_large(tmp_path):
    pipeline = StreamingAgentPipeline(
        TranslatorConfig(api_key="test-key", model_name="m", summary_model_name="m")
    )
    report = AgentReport("in.srt", "out.srt")
    detail_path = tmp_path / "failure-events.ndjson"
    pipeline._detail_paths = {"failures": detail_path}

    for index in range(501):
        pipeline._failed_block(_block(index), report, "invalid response")

    report_path = tmp_path / "out.agent-report.json"
    pipeline._persist_detail_files(report, report_path)

    assert len(report.failures) == 500
    assert report.stats["failure_count"] == 501
    assert Path(report.detail_files["failures"]).exists()


@pytest.mark.asyncio
async def test_audit_uses_character_rules_without_glossary(tmp_path):
    reviewed = tmp_path / "reviewed.ndjson"
    audited = tmp_path / "audited.ndjson"
    block = _block(0, "Alice, wait.")
    append_block_spool(reviewed, block.copy(entry=block.entry.copy(text="爱丽丝，等等"), stage="reviewed"))
    plan = AgentPlan(character_voices={"Alice": "统一译为爱丽丝"})
    pipeline = StreamingAgentPipeline(
        TranslatorConfig(api_key="test-key", model_name="m", summary_model_name="m")
    )
    report = AgentReport("in.srt", "out.srt")

    with patch("srt_translator.streaming_pipeline.call_llm_async", new_callable=AsyncMock) as call:
        call.return_value = '{"corrections":[]}'
        await pipeline._audit_consistency(
            reviewed,
            audited,
            plan,
            MagicMock(),
            report,
            None,
            None,
        )

    call.assert_awaited_once()
    assert report.audit["checked_blocks"] == 1


@pytest.mark.asyncio
async def test_pipeline_streams_ten_thousand_blocks_with_bounded_tasks(tmp_path):
    input_path = tmp_path / "long.srt"
    input_path.write_text("synthetic input", encoding="utf-8")
    output_path = tmp_path / "translated_long.srt"
    report_path = tmp_path / "translated_long.agent-report.json"
    window_sizes: list[int] = []
    chunk_sizes: list[int] = []
    config = TranslatorConfig(
        api_key="test-key",
        model_name="m",
        summary_model_name="m",
        chunk_size=10,
        analysis_window_entries=120,
        analysis_window_max_chars=9000,
    )

    def fake_build(_input_path, spool_path, *_args):
        return write_block_spool(
            spool_path,
            (_block(index, f"line {index}") for index in range(10_000)),
        )

    async def fake_analyze(_client, _model, window_id, blocks, _config, custom_prompt=None):
        window_sizes.append(len(blocks))
        return AgentWindowAnalysis(
            window_id,
            blocks[0].block_id,
            blocks[-1].block_id,
            summary=f"window {window_id}",
        )

    async def fake_translate(**kwargs):
        chunk_sizes.append(len(kwargs["chunk_data"]))
        return [
            TranslationResult(item["index"], item["text"], f"译文 {item['index']}", True)
            for item in kwargs["chunk_data"]
        ]

    async def copy_review(source, target, *_args):
        return write_block_spool(target, iter_block_spool(source))

    with patch("srt_translator.streaming_pipeline.build_block_spool", side_effect=fake_build), \
         patch("srt_translator.streaming_pipeline.analyze_window", side_effect=fake_analyze), \
         patch("srt_translator.streaming_pipeline.reduce_agent_plan", new_callable=AsyncMock) as reduce, \
         patch("srt_translator.streaming_pipeline.translate_chunk_task", side_effect=fake_translate), \
         patch.object(StreamingAgentPipeline, "_review_timeline", side_effect=copy_review), \
         patch.object(StreamingAgentPipeline, "_audit_consistency", side_effect=copy_review):
        reduce.return_value = AgentPlan(style_guide="简洁")
        result = await StreamingAgentPipeline(config).run_file(
            input_path,
            output_path,
            report_path,
            Glossary(),
            MagicMock(),
            spool_dir=tmp_path / "spool",
        )

    assert result.block_count == 10_000
    assert result.translated_count == 10_000
    assert max(window_sizes) <= 120
    assert max(chunk_sizes) <= 10
    assert output_path.read_text(encoding="utf-8").count("-->") == 10_000
