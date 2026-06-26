"""Core translation logic using LLM."""

from __future__ import annotations

import asyncio
import json
import re
import logging
from typing import List, Dict, Any, Optional, Mapping
from dataclasses import dataclass

from .llm_client import LLMCallError, call_llm_async
from .glossary import find_matching_terms, Glossary
from .prompts import (
    ANALYSIS_BRIEF_SYSTEM_PROMPT,
    NBL_AGENT_PROTOCOL,
    REVIEW_SYSTEM_PROMPT,
    SINGLE_RETRY_SYSTEM_PROMPT,
    TRANSLATION_SYSTEM_PROMPT,
)
from .text_utils import validate_translation

logger = logging.getLogger(__name__)


@dataclass
class TranslationResult:
    """单条翻译结果。"""
    index: int
    original: str
    translated: str
    success: bool
    error: str = ""


def _sample_full_text(full_text: str, max_len: int = 9000, num_segments: int = 6) -> str:
    """Uniformly sample long subtitle text while preserving coverage."""
    if len(full_text) <= max_len:
        return full_text

    lines = full_text.split("\n")
    if len(lines) < num_segments * 2:
        return full_text[: max_len // 2] + "\n...\n" + full_text[-max_len // 2 :]

    segments = []
    max_per_segment = max_len // num_segments
    for i in range(num_segments):
        start_idx = int(i * len(lines) / num_segments)
        end_idx = int((i + 1) * len(lines) / num_segments)
        segment_text = "\n".join(lines[start_idx:end_idx])
        if len(segment_text) > max_per_segment:
            half = max_per_segment // 2
            segment_text = segment_text[:half] + "\n...\n" + segment_text[-half:]
        segments.append(segment_text)
    return "\n".join(segments)


async def generate_agent_brief(
    full_text: str,
    client: Any,
    model: str,
    custom_prompt: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """
    Generate the first-turn agent brief for the full subtitle task.
    
    The brief is reused by later translation and review turns so the
    agent has one stable view of topic, tone, terminology, and risks.
    
    Args:
        full_text: The full text to analyze
        client: DeepSeek client
        model: Model name to use
        custom_prompt: Optional custom system prompt to override the default
    """
    if not full_text.strip():
        return ""
    
    logger.info(f"Generating agent translation brief using model: {model}...")
    
    if custom_prompt:
        system_prompt = f"""{custom_prompt}

{NBL_AGENT_PROTOCOL}

输出使用中文，控制在 500 字以内，直接给出策略，不要寒暄。"""
    else:
        system_prompt = ANALYSIS_BRIEF_SYSTEM_PROMPT

    sampled = _sample_full_text(full_text)
    
    content = await call_llm_async(
        client,
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": sampled}
        ],
        max_retries=2,
        max_tokens=max_tokens,
    )
    
    if content:
        logger.info(f"Agent brief generated ({len(content)} chars)")
    else:
        logger.warning("Failed to generate agent brief")
    
    return content


async def generate_context_summary(
    full_text: str,
    client: Any,
    model: str,
    custom_prompt: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Backward-compatible alias for the agent brief stage."""
    return await generate_agent_brief(
        full_text,
        client,
        model,
        custom_prompt=custom_prompt,
        max_tokens=max_tokens,
    )


def _build_translation_prompt(
    items: List[Dict[str, Any]],
    context_prev: List[str],
    context_next: List[str],
    agent_brief: str,
    matched_terms: List[str],
    custom_prompt: str | None = None,
    translation_memory: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Build translation prompts.

    Args:
        items: Items to translate.
        context_prev: Previous context text.
        context_next: Next context text.
        agent_brief: Full-task agent translation strategy.
        matched_terms: Glossary term matches.
        custom_prompt: Optional custom system prompt.
        translation_memory: Dict mapping original text -> translated text
            from previously completed chunks, used for terminology consistency.
    """

    if custom_prompt:
        system_prompt = f"""{custom_prompt}

{NBL_AGENT_PROTOCOL}

无论自定义要求如何，必须只输出合法 JSON：
{{"translations": [{{"id": 0, "text": "翻译"}}, ...]}}
条目数量必须与输入完全一致。"""
    else:
        system_prompt = TRANSLATION_SYSTEM_PROMPT
    
    glossary_section = ""
    if matched_terms:
        glossary_list = "\n".join([f"  - {t}" for t in matched_terms])
        glossary_section = f"\n## 术语表（必须使用）：\n{glossary_list}\n"

    # Translation memory section: show up to 5 previously translated pairs
    memory_section = ""
    if translation_memory:
        # Pick up to 5 representative entries from memory
        memory_items = list(translation_memory.items())[-5:]
        memory_lines = "\n".join(
            [f"  {orig} -> {trans}" for orig, trans in memory_items]
        )
        memory_section = f"\n## 翻译记忆（参考已有翻译，保持术语一致）：\n{memory_lines}\n"

    prev_str = " | ".join(context_prev[-3:]) if context_prev else ""
    next_str = " | ".join(context_next[:3]) if context_next else ""
    
    context_section = ""
    if prev_str or next_str:
        context_section = f"\n## 上下文：\n前文：{prev_str or 'N/A'}\n后文：{next_str or 'N/A'}\n"
    
    brief_section = ""
    if agent_brief:
        brief_section = f"\n## Agent 翻译策略：\n{agent_brief[:1200]}\n"
    
    user_prompt = f"""{brief_section}{memory_section}{glossary_section}{context_section}
## 请翻译以下内容：
{json.dumps(items, ensure_ascii=False)}

只输出 JSON，不要其他内容："""
    
    return system_prompt, user_prompt


def _parse_translation_response(json_str: str, expected_count: int) -> Dict[int, str]:
    """Parse JSON response from translation API.

    兼容多种返回格式:
    1. {"translations": [{"id": 0, "text": "..."}, ...]}  (dict with translations key)
    2. [{"id": 0, "text": "..."}, ...]                      (plain array)
    3. JSON 后带有额外文本（Extra data），自动忽略多余内容
    """
    
    if not json_str:
        return {}
    
    translated_map: Dict[int, str] = {}
    
    try:
        # 清理可能的 markdown 格式
        clean = json_str.strip()
        clean = re.sub(r'^```(?:json)?\s*', '', clean)
        clean = re.sub(r'\s*```$', '', clean)
        
        # 使用 raw_decode 处理可能带有额外文本的 JSON
        # 例如模型返回: {"translations":[...]}后面还有文字
        # raw_decode 会只解析第一个完整的 JSON 值，忽略后续内容
        decoder = json.JSONDecoder()
        try:
            data, _ = decoder.raw_decode(clean)
        except json.JSONDecodeError:
            # 如果 raw_decode 也失败，回退到普通 json.loads
            data = json.loads(clean)
        
        # 兼容两种顶层格式
        if isinstance(data, dict):
            translations = data.get("translations", [])
            if not isinstance(translations, list):
                logger.warning("'translations' is not a list")
                return {}
        elif isinstance(data, list):
            # 模型直接返回了纯数组格式
            translations = data
        else:
            logger.warning(f"Unexpected JSON type: {type(data).__name__}")
            return {}
        
        for item in translations:
            if not isinstance(item, dict):
                continue
            
            item_id = item.get("id")
            text = item.get("text", "")
            
            # 验证 id 在有效范围内
            if isinstance(item_id, int) and 0 <= item_id < expected_count:
                translated_map[item_id] = str(text)
            else:
                logger.debug(f"Invalid id: {item_id}")
                
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed: {e}")
        logger.debug(f"Raw response: {json_str[:200]}...")
    
    return translated_map


async def _translate_single_retry(
    client: Any,
    model: str,
    original: str,
    agent_brief: str,
    glossary: Glossary | Dict[str, str],
    context_prev: List[str] | None = None,
    context_next: List[str] | None = None,
    max_tokens: int | None = None,
) -> str:
    """单条翻译重试（用于 chunk 翻译失败的条目）。
    
    Args:
        client: DeepSeek client.
        model: Model name.
        original: Original text to translate.
        agent_brief: Full-task agent translation strategy.
        glossary: Glossary for terminology.
        context_prev: Previous subtitle texts for context.
        context_next: Next subtitle texts for context.
    """
    
    matched = find_matching_terms(glossary, original)
    terms = [f"{k} -> {v}" for k, v in matched.items()]
    
    system_prompt = SINGLE_RETRY_SYSTEM_PROMPT
    
    glossary_hint = f" 术语：{', '.join(terms)}" if terms else ""
    
    # Build context-aware user prompt
    context_lines = []
    if agent_brief:
        context_lines.append(f"[Agent翻译策略]：{agent_brief[:400]}")
    if context_prev:
        prev_text = " | ".join(context_prev[-3:])
        context_lines.append(f"[前文]：{prev_text}")
    if context_next:
        next_text = " | ".join(context_next[:3])
        context_lines.append(f"[后文]：{next_text}")
    
    context_str = "\n".join(context_lines)
    if context_str:
        context_str += "\n"
    
    if glossary_hint:
        context_str += glossary_hint + "\n"
    
    user_prompt = f"""{context_str}请翻译以下句子：
{original}"""
    
    result = await call_llm_async(
        client, model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        max_retries=2,
        json_mode=False,
        max_tokens=max_tokens,
    )
    
    return result.strip() if result else ""


async def _review_chunk_results(
    client: Any,
    model: str,
    chunk_data: List[Dict[str, Any]],
    draft_results: List[TranslationResult],
    context_prev: List[str],
    context_next: List[str],
    agent_brief: str,
    matched_terms: List[str],
    max_tokens: int | None,
) -> List[TranslationResult]:
    """Agent review turn: polish and repair a translated chunk."""
    review_items = []
    for local_id, (item, draft) in enumerate(zip(chunk_data, draft_results)):
        review_items.append(
            {
                "id": local_id,
                "index": item["index"],
                "original": item["text"],
                "draft": draft.translated if draft.success else "",
            }
        )

    prev_str = " | ".join(context_prev[-4:]) if context_prev else ""
    next_str = " | ".join(context_next[:4]) if context_next else ""
    glossary_section = ""
    if matched_terms:
        glossary_section = "\n## 术语表（必须使用）：\n" + "\n".join(
            [f"  - {term}" for term in matched_terms]
        )

    system_prompt = REVIEW_SYSTEM_PROMPT

    user_prompt = f"""## Agent 翻译策略：
{agent_brief[:1200] if agent_brief else "无"}
{glossary_section}

## 上下文：
前文：{prev_str or "N/A"}
后文：{next_str or "N/A"}

## 请复审以下字幕草稿：
{json.dumps(review_items, ensure_ascii=False)}

只输出 JSON，不要解释："""

    try:
        json_str = await call_llm_async(
            client,
            model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_retries=2,
            json_mode=True,
            max_tokens=max_tokens,
        )
    except LLMCallError:
        raise
    except Exception as exc:
        logger.warning("Agent review failed, keeping draft chunk: %s", exc)
        return draft_results

    reviewed_map = _parse_translation_response(json_str, len(chunk_data))
    if not reviewed_map:
        logger.warning("Agent review returned no usable corrections, keeping draft chunk")
        return draft_results

    reviewed: List[TranslationResult] = []
    for i, item in enumerate(chunk_data):
        original = item["text"]
        fallback = draft_results[i]
        candidate = reviewed_map.get(i, fallback.translated)
        is_valid, error = validate_translation(original, candidate)
        if is_valid:
            reviewed.append(
                TranslationResult(
                    index=item["index"],
                    original=original,
                    translated=candidate,
                    success=True,
                )
            )
        else:
            reviewed.append(fallback)
            logger.debug("Agent review correction rejected for #%s: %s", i, error)

    return reviewed


async def translate_chunk_task(
    client: Any,
    chunk_data: List[Dict[str, Any]],
    context_prev: List[str],
    context_next: List[str],
    agent_brief: str,
    glossary: Glossary | Dict[str, str],
    model: str,
    sem: asyncio.Semaphore,
    retry_failed: bool = True,
    custom_translation_prompt: str | None = None,
    translation_memory: Mapping[str, str] | None = None,
    max_tokens: int | None = 4096,
) -> List[TranslationResult]:
    """
    Translate a chunk of subtitle entries with an agent draft + review turn.

    Args:
        translation_memory: Dict of original -> translated from previous chunks
            for terminology consistency across chunks.

    Returns:
        List of TranslationResult objects
    """
    async with sem:
        items = [
            {"id": i, "text": item['text']}
            for i, item in enumerate(chunk_data)
        ]
        
        # 动态术语匹配
        chunk_text = " ".join([item['text'] for item in chunk_data])
        matched = find_matching_terms(glossary, chunk_text)
        matched_terms = [f"{term} -> {trans}" for term, trans in matched.items()]
        
        system_prompt, user_prompt = _build_translation_prompt(
            items, context_prev, context_next, agent_brief, matched_terms,
            custom_prompt=custom_translation_prompt,
            translation_memory=translation_memory,
        )
        
        json_str = await call_llm_async(
            client, model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_retries=3,
            json_mode=True,
            max_tokens=max_tokens,
        )
        
        translated_map = _parse_translation_response(json_str, len(chunk_data))
        
        # 构建结果
        results: List[TranslationResult] = []
        failed_indices: List[int] = []
        
        for i, item in enumerate(chunk_data):
            original = item['text']
            
            if i in translated_map:
                translated = translated_map[i]
                is_valid, error = validate_translation(original, translated)
                
                if is_valid:
                    results.append(TranslationResult(
                        index=item['index'],
                        original=original,
                        translated=translated,
                        success=True
                    ))
                else:
                    logger.debug(f"Validation failed for #{i}: {error}")
                    failed_indices.append(i)
                    results.append(TranslationResult(
                        index=item['index'],
                        original=original,
                        translated="",
                        success=False,
                        error=error
                    ))
            else:
                failed_indices.append(i)
                results.append(TranslationResult(
                    index=item['index'],
                    original=original,
                    translated="",
                    success=False,
                    error="Missing from response"
                ))
        
        # 并行重试失败的项（带上下文）
        if retry_failed and failed_indices:
            logger.info(f"Retrying {len(failed_indices)} failed items individually...")

            async def _retry_one(i: int) -> tuple[int, str]:
                original = chunk_data[i]['text']
                retried = await _translate_single_retry(
                    client, model, original, agent_brief, glossary,
                    context_prev=context_prev,
                    context_next=context_next,
                    max_tokens=max_tokens,
                )
                return i, retried

            retry_results = await asyncio.gather(*[_retry_one(i) for i in failed_indices])

            for i, retried in retry_results:
                if retried:
                    is_valid, _ = validate_translation(chunk_data[i]['text'], retried)
                    if is_valid:
                        results[i] = TranslationResult(
                            index=chunk_data[i]['index'],
                            original=chunk_data[i]['text'],
                            translated=retried,
                            success=True,
                        )
                        logger.debug(f"Retry succeeded for #{i}")
        
        reviewed_results = await _review_chunk_results(
            client=client,
            model=model,
            chunk_data=chunk_data,
            draft_results=results,
            context_prev=context_prev,
            context_next=context_next,
            agent_brief=agent_brief,
            matched_terms=matched_terms,
            max_tokens=max_tokens,
        )
        return reviewed_results
