"""Agent planning and reporting utilities for streaming translation."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import TranslatorConfig
from .glossary import Glossary
from .llm_client import call_llm_async
from .models import SubtitleBlock
from .prompts import (
    DEFAULT_STYLE_GUIDE,
    NBL_AGENT_PROTOCOL,
    PLAN_REDUCER_SYSTEM_PROMPT,
    WINDOW_ANALYSIS_SYSTEM_PROMPT,
)
from .streaming import block_texts

logger = logging.getLogger(__name__)


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Parse the first JSON object from an LLM response."""
    if not raw:
        return {}
    clean = raw.strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean)
    try:
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(clean)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON object from model response")
        return {}


def _terms_to_dict(value: Any) -> dict[str, str]:
    """Normalize model/user term data into a string dictionary."""
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if str(k).strip() and str(v).strip()}
    if isinstance(value, list):
        terms: dict[str, str] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            source = item.get("source") or item.get("term") or item.get("original")
            target = item.get("translation") or item.get("target") or item.get("text")
            if source and target:
                terms[str(source)] = str(target)
        return terms
    return {}


def _clip(value: Any, limit: int) -> str:
    """Bound free-form model text retained in plans and prompts."""
    return str(value or "").strip()[:limit]


@dataclass
class AgentWindowAnalysis:
    """Structured planning result for one subtitle window."""

    window_id: int
    block_start: int
    block_end: int
    summary: str = ""
    style: str = ""
    characters: dict[str, str] = field(default_factory=dict)
    terms: dict[str, str] = field(default_factory=dict)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "block_start": self.block_start,
            "block_end": self.block_end,
            "summary": self.summary,
            "style": self.style,
            "characters": self.characters,
            "terms": self.terms,
            "risks": self.risks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentWindowAnalysis":
        return cls(
            window_id=int(data["window_id"]),
            block_start=int(data["block_start"]),
            block_end=int(data["block_end"]),
            summary=_clip(data.get("summary", ""), 1200),
            style=_clip(data.get("style", ""), 500),
            characters={
                _clip(k, 80): _clip(v, 160)
                for k, v in list((data.get("characters") or {}).items())[:80]
            } if isinstance(data.get("characters"), dict) else {},
            terms=_terms_to_dict(data.get("terms")),
            risks=[_clip(item, 300) for item in data.get("risks", [])[:40] if str(item).strip()]
            if isinstance(data.get("risks"), list) else [],
        )


@dataclass
class PlanFragment:
    """Bounded planning material reduced through multiple hierarchy levels."""

    block_start: int
    block_end: int
    summary: str = ""
    style: str = ""
    characters: dict[str, str] = field(default_factory=dict)
    terms: dict[str, str] = field(default_factory=dict)
    risks: list[str] = field(default_factory=list)

    @classmethod
    def from_analysis(cls, analysis: AgentWindowAnalysis) -> "PlanFragment":
        return cls(
            block_start=analysis.block_start,
            block_end=analysis.block_end,
            summary=analysis.summary,
            style=analysis.style,
            characters=dict(analysis.characters),
            terms=dict(analysis.terms),
            risks=list(analysis.risks),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_start": self.block_start,
            "block_end": self.block_end,
            "summary": self.summary,
            "style": self.style,
            "characters": self.characters,
            "terms": self.terms,
            "risks": self.risks,
        }


@dataclass
class AgentPlan:
    """Global translation plan reduced from all window analyses."""

    style_guide: str = ""
    glossary: dict[str, str] = field(default_factory=dict)
    character_voices: dict[str, str] = field(default_factory=dict)
    window_summaries: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "style_guide": self.style_guide,
            "glossary": self.glossary,
            "character_voices": self.character_voices,
            "window_summaries": self.window_summaries,
            "risks": self.risks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentPlan":
        return cls(
            style_guide=str(data.get("style_guide", "")),
            glossary=_terms_to_dict(data.get("glossary")),
            character_voices={
                str(k): str(v)
                for k, v in (data.get("character_voices") or {}).items()
            } if isinstance(data.get("character_voices"), dict) else {},
            window_summaries=list(data.get("window_summaries") or []),
            risks=[str(item) for item in data.get("risks", []) if str(item).strip()]
            if isinstance(data.get("risks"), list) else [],
        )

    def brief_for_block(self, block_id: int) -> str:
        """Build a compact plan brief for a specific block."""
        matched_summary = ""
        local_voices: dict[str, str] = {}
        local_risks: list[str] = []
        for summary in self.window_summaries:
            if summary.get("block_start", -1) <= block_id <= summary.get("block_end", -1):
                matched_summary = str(summary.get("summary", ""))
                local_voices = _terms_to_dict(summary.get("characters"))
                local_risks = [
                    str(item) for item in summary.get("risks", []) if str(item).strip()
                ]
                break
        terms = "\n".join(
            f"- {source} -> {target}"
            for source, target in list(self.glossary.items())[:80]
        )
        voices_map = dict(self.character_voices)
        voices_map.update(local_voices)
        voices = "\n".join(
            f"- {name}: {voice}"
            for name, voice in list(voices_map.items())[:40]
        )
        risks = "\n".join(f"- {risk}" for risk in [*local_risks, *self.risks][:40])
        return f"""风格策略：
{self.style_guide}

当前窗口摘要：
{matched_summary}

人物/称呼：
{voices or "无"}

术语表：
{terms or "无"}

风险点：
{risks or "无"}"""


@dataclass
class AgentReport:
    """Run report emitted next to translated subtitles."""

    input_file: str
    output_file: str
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: str | None = None
    status: str = "running"
    error: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    agent_plan: dict[str, Any] = field(default_factory=dict)
    window_analyses: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    detail_files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_file": self.input_file,
            "output_file": self.output_file,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "error": self.error,
            "stats": self.stats,
            "agent_plan": self.agent_plan,
            "window_analyses": self.window_analyses,
            "failures": self.failures,
            "retries": self.retries,
            "risks": self.risks,
            "audit": self.audit,
            "detail_files": self.detail_files,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)


def glossary_to_dict(glossary: Glossary) -> dict[str, str]:
    """Extract a plain dictionary from a glossary."""
    return glossary.to_dict() if isinstance(glossary, Glossary) else {}


def _fallback_window_analysis(window_id: int, blocks: list[SubtitleBlock]) -> AgentWindowAnalysis:
    """Create a conservative analysis when the model response is unusable."""
    summary_text = " / ".join(block.entry.text for block in blocks[:5])
    return AgentWindowAnalysis(
        window_id=window_id,
        block_start=blocks[0].block_id,
        block_end=blocks[-1].block_id,
        summary=summary_text[:500],
        style=DEFAULT_STYLE_GUIDE,
        risks=["窗口分析回退：模型未返回有效结构化 JSON"],
    )


async def analyze_window(
    client: Any,
    model: str,
    window_id: int,
    blocks: list[SubtitleBlock],
    config: TranslatorConfig,
    custom_prompt: str | None = None,
) -> AgentWindowAnalysis:
    """Analyze one subtitle window into structured translation guidance."""
    if not blocks:
        raise ValueError("Cannot analyze an empty subtitle window")

    system_prompt = custom_prompt or WINDOW_ANALYSIS_SYSTEM_PROMPT
    if custom_prompt:
        system_prompt = f"""{custom_prompt}

{NBL_AGENT_PROTOCOL}

无论自定义要求如何，必须只输出合法 JSON，不要解释。"""

    user_prompt = f"""请分析以下字幕窗口，并输出：
{{
  "summary": "本窗口内容摘要",
  "style": "本窗口翻译风格建议，说明如何让普通中文观众容易看懂",
  "characters": {{"人物或说话者": "称呼/语气建议"}},
  "terms": [{{"source": "英文术语", "translation": "建议中文译法"}}],
  "risks": ["双关、梗、专有设定、上下文依赖或易错点"]
}}

字幕窗口：
{block_texts(blocks)}

只输出 JSON："""

    raw = await call_llm_async(
        client,
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_retries=2,
        json_mode=True,
        max_tokens=min(config.max_output_tokens, 2048),
    )
    data = _parse_json_object(raw)
    if not data:
        return _fallback_window_analysis(window_id, blocks)

    data["window_id"] = window_id
    data["block_start"] = blocks[0].block_id
    data["block_end"] = blocks[-1].block_id
    return AgentWindowAnalysis.from_dict(data)


def _merge_window_terms(
    analyses: Iterable[AgentWindowAnalysis],
    user_terms: dict[str, str],
) -> dict[str, str]:
    """Merge terms with user glossary priority and frequency fallback."""
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    original_case: dict[str, str] = {}
    for analysis in analyses:
        for source, target in analysis.terms.items():
            source_clean = source.strip()
            target_clean = target.strip()
            if not source_clean or not target_clean:
                continue
            key = source_clean.lower()
            original_case.setdefault(key, source_clean)
            counts[key][target_clean] += 1

    merged: dict[str, str] = {}
    for key, counter in counts.items():
        merged[original_case[key]] = counter.most_common(1)[0][0]

    # User glossary wins over agent-derived terms.
    lower_to_user = {key.lower(): (key, value) for key, value in user_terms.items()}
    for existing in list(merged):
        if existing.lower() in lower_to_user:
            del merged[existing]
    merged.update(user_terms)
    return merged


def _bounded_fragment_dict(fragment: PlanFragment, budget: int) -> dict[str, Any]:
    """Return a compact representation that is safe to place in a reduce prompt."""
    data = {
        "block_start": fragment.block_start,
        "block_end": fragment.block_end,
        "summary": _clip(fragment.summary, max(100, budget // 3)),
        "style": _clip(fragment.style, max(60, budget // 8)),
        "characters": {
            _clip(k, 60): _clip(v, 100)
            for k, v in list(fragment.characters.items())[:12]
        },
        "terms": {
            _clip(k, 60): _clip(v, 80)
            for k, v in list(fragment.terms.items())[:16]
        },
        "risks": [_clip(risk, 140) for risk in fragment.risks[:10]],
    }
    while len(json.dumps(data, ensure_ascii=False)) > budget:
        if data["risks"]:
            data["risks"].pop()
        elif data["terms"]:
            data["terms"].pop(next(iter(data["terms"])))
        elif data["characters"]:
            data["characters"].pop(next(iter(data["characters"])))
        elif len(data["summary"]) > 80:
            data["summary"] = data["summary"][: len(data["summary"]) // 2]
        else:
            break
    return data


def _iter_fragment_groups(
    fragments: list[PlanFragment],
    budget: int,
) -> Iterable[list[PlanFragment]]:
    """Group fragments for a reduce call without head/tail truncation."""
    per_item_budget = max(220, budget // 3)
    group: list[PlanFragment] = []
    group_size = 2
    for fragment in fragments:
        size = len(
            json.dumps(_bounded_fragment_dict(fragment, per_item_budget), ensure_ascii=False)
        )
        if group and group_size + size + 2 > budget:
            yield group
            group = []
            group_size = 2
        group.append(fragment)
        group_size += size + 1
    if group:
        yield group


def _merge_fragment_values(fragments: list[PlanFragment]) -> PlanFragment:
    """Create a deterministic fallback fragment for one hierarchy group."""
    characters: dict[str, str] = {}
    terms: dict[str, str] = {}
    risks: list[str] = []
    for fragment in fragments:
        characters.update(fragment.characters)
        terms.update(fragment.terms)
        risks.extend(fragment.risks)
    return PlanFragment(
        block_start=fragments[0].block_start,
        block_end=fragments[-1].block_end,
        summary=_clip(" / ".join(fragment.summary for fragment in fragments if fragment.summary), 1200),
        style=_clip("；".join(fragment.style for fragment in fragments if fragment.style), 500),
        characters=characters,
        terms=terms,
        risks=risks[:80],
    )


async def _reduce_fragment_group(
    client: Any,
    model: str,
    fragments: list[PlanFragment],
    config: TranslatorConfig,
) -> PlanFragment:
    """Reduce one bounded group, preserving deterministic fallback data."""
    fallback = _merge_fragment_values(fragments)
    budget = max(600, config.analysis_window_max_chars)
    payload = [
        _bounded_fragment_dict(fragment, max(220, budget // 3))
        for fragment in fragments
    ]
    system_prompt = PLAN_REDUCER_SYSTEM_PROMPT
    user_prompt = f"""待归并片段：
{json.dumps(payload, ensure_ascii=False)}

输出：
{{
  "summary": "覆盖这些片段的摘要",
  "style": "风格与语气策略，包含通俗表达和避免翻译腔的要求",
  "characters": {{"人物或说话者": "稳定称呼/语气"}},
  "risks": ["易错点"]
}}

只输出 JSON："""
    raw = await call_llm_async(
        client,
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_retries=2,
        json_mode=True,
        max_tokens=min(config.max_output_tokens, 2048),
    )
    data = _parse_json_object(raw)
    characters = (
        _terms_to_dict(data.get("characters") or data.get("character_voices"))
        if data
        else {}
    )
    risks = data.get("risks") if data else None
    return PlanFragment(
        block_start=fallback.block_start,
        block_end=fallback.block_end,
        summary=_clip(data.get("summary") if data else fallback.summary, 1200) or fallback.summary,
        style=_clip(
            (data.get("style") or data.get("style_guide")) if data else fallback.style,
            500,
        ) or fallback.style,
        characters=characters or fallback.characters,
        terms=fallback.terms,
        risks=(
            [_clip(item, 300) for item in risks[:80] if str(item).strip()]
            if isinstance(risks, list) and risks
            else fallback.risks
        ),
    )


async def reduce_agent_plan(
    client: Any,
    model: str,
    analyses: Iterable[AgentWindowAnalysis],
    user_glossary: Glossary,
    config: TranslatorConfig,
) -> AgentPlan:
    """Hierarchically reduce window analyses into a global agent plan."""
    user_terms = glossary_to_dict(user_glossary)
    leaves: list[PlanFragment] = []
    window_summaries: list[dict[str, Any]] = []
    term_counts: dict[str, Counter[str]] = defaultdict(Counter)
    original_case: dict[str, str] = {}

    for analysis in analyses:
        leaves.append(PlanFragment.from_analysis(analysis))
        window_summaries.append(
            {
                "window_id": analysis.window_id,
                "block_start": analysis.block_start,
                "block_end": analysis.block_end,
                "summary": analysis.summary,
                "characters": analysis.characters,
                "risks": analysis.risks,
            }
        )
        for source, target in analysis.terms.items():
            key = source.strip().lower()
            if key and target.strip():
                original_case.setdefault(key, source.strip())
                term_counts[key][target.strip()] += 1

    if not leaves:
        return AgentPlan(glossary=user_terms)

    merged_terms = {
        original_case[key]: counts.most_common(1)[0][0]
        for key, counts in term_counts.items()
    }
    user_keys = {key.lower() for key in user_terms}
    merged_terms = {
        key: value for key, value in merged_terms.items() if key.lower() not in user_keys
    }
    merged_terms.update(user_terms)

    fragments = leaves
    budget = max(600, config.analysis_window_max_chars)
    while len(fragments) > 1:
        groups = list(_iter_fragment_groups(fragments, budget))
        if len(groups) == len(fragments):
            groups = [fragments[idx:idx + 2] for idx in range(0, len(fragments), 2)]
        reduced: list[PlanFragment] = []
        for group in groups:
            reduced.append(await _reduce_fragment_group(client, model, group, config))
        fragments = reduced

    root = fragments[0]
    if len(leaves) == 1:
        root = await _reduce_fragment_group(client, model, leaves, config)
    return AgentPlan(
        style_guide=root.style
        or DEFAULT_STYLE_GUIDE,
        glossary=merged_terms,
        character_voices=root.characters,
        window_summaries=window_summaries,
        risks=root.risks[:80],
    )
