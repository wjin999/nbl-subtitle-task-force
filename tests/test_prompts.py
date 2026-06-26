"""Prompt contract tests for NBL Subtitle Task Force."""

from __future__ import annotations

from srt_translator.prompts import (
    ANALYSIS_BRIEF_SYSTEM_PROMPT,
    CONSISTENCY_AUDIT_SYSTEM_PROMPT,
    NBL_AGENT_PROTOCOL,
    PROJECT_NAME,
    PLAN_REDUCER_SYSTEM_PROMPT,
    PROJECT_NAME_CN,
    PROJECT_NAME_EN,
    PROJECT_REPO_SLUG,
    REVIEW_SYSTEM_PROMPT,
    TIMELINE_REVIEW_SYSTEM_PROMPT,
    TRANSLATION_SYSTEM_PROMPT,
    WINDOW_ANALYSIS_SYSTEM_PROMPT,
)


def test_project_names_are_nbl_subtitle_task_force():
    assert PROJECT_NAME_CN == "NBL Subtitle Task Force"
    assert PROJECT_NAME_EN == "NBL Subtitle Task Force"
    assert PROJECT_NAME == "NBL Subtitle Task Force"
    assert PROJECT_REPO_SLUG == "nbl-subtitle-task-force"


def test_agent_stage_prompts_share_nbl_workflow_contract():
    prompts = [
        ANALYSIS_BRIEF_SYSTEM_PROMPT,
        WINDOW_ANALYSIS_SYSTEM_PROMPT,
        PLAN_REDUCER_SYSTEM_PROMPT,
        TRANSLATION_SYSTEM_PROMPT,
        REVIEW_SYSTEM_PROMPT,
        TIMELINE_REVIEW_SYSTEM_PROMPT,
        CONSISTENCY_AUDIT_SYSTEM_PROMPT,
    ]

    for prompt in prompts:
        assert PROJECT_NAME_CN in prompt
        assert "NBL Subtitle Task Force 工作协议" in prompt
        assert "输出契约优先" in prompt
        assert "质量自检" in prompt
        assert "失败透明" in prompt


def test_translation_prompt_keeps_json_and_subtitle_contracts():
    assert NBL_AGENT_PROTOCOL in TRANSLATION_SYSTEM_PROMPT
    assert '{"translations":' in TRANSLATION_SYSTEM_PROMPT
    assert "条目数量必须与输入完全一致" in TRANSLATION_SYSTEM_PROMPT
    assert "句末不加句号" in TRANSLATION_SYSTEM_PROMPT
