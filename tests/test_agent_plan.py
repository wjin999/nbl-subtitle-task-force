"""Tests for agent planning helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srt_translator.agent_plan import AgentWindowAnalysis, reduce_agent_plan
from srt_translator.config import TranslatorConfig
from srt_translator.glossary import Glossary


@pytest.mark.asyncio
async def test_reduce_agent_plan_user_glossary_wins():
    analyses = [
        AgentWindowAnalysis(
            window_id=0,
            block_start=0,
            block_end=10,
            terms={"Night City": "夜城", "runner": "跑者"},
            summary="cyberpunk setup",
        ),
        AgentWindowAnalysis(
            window_id=1,
            block_start=11,
            block_end=20,
            terms={"Night City": "夜之城"},
            summary="later conflict",
        ),
    ]
    glossary = Glossary()
    glossary.add("Night City", "夜之城")
    config = TranslatorConfig(api_key="test-key", model_name="m", summary_model_name="m")

    with patch("srt_translator.agent_plan.call_llm_async", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = '{"style_guide":"口语化","character_voices":{},"risks":[]}'
        plan = await reduce_agent_plan(MagicMock(), "m", analyses, glossary, config)

    assert plan.glossary["Night City"] == "夜之城"
    assert plan.glossary["runner"] == "跑者"
    assert plan.style_guide == "口语化"
    assert len(plan.window_summaries) == 2


@pytest.mark.asyncio
async def test_reduce_agent_plan_hierarchically_preserves_middle_windows():
    analyses = [
        AgentWindowAnalysis(
            window_id=index,
            block_start=index * 10,
            block_end=index * 10 + 9,
            summary=(f"window-{index}-" + "x" * 450),
            characters={"Middle": "保持称呼"} if index == 3 else {},
            risks=["中段风险"] if index == 3 else [],
        )
        for index in range(8)
    ]
    config = TranslatorConfig(
        api_key="test-key",
        model_name="m",
        summary_model_name="m",
        analysis_window_max_chars=1000,
    )

    with patch("srt_translator.agent_plan.call_llm_async", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "{}"
        plan = await reduce_agent_plan(MagicMock(), "m", analyses, Glossary(), config)

    prompts = "\n".join(call.args[2][1]["content"] for call in mock_call.await_args_list)
    assert mock_call.await_count > 1
    assert "window-3-" in prompts
    assert "\n...\n" not in prompts
    assert plan.character_voices["Middle"] == "保持称呼"
    assert "中段风险" in plan.risks
