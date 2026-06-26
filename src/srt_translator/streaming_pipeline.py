"""Streaming, resumable agent subtitle translation pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Iterator

from .agent_plan import (
    AgentPlan,
    AgentReport,
    AgentWindowAnalysis,
    _merge_window_terms,
    _parse_json_object,
    analyze_window,
    reduce_agent_plan,
)
from .config import TranslatorConfig
from .glossary import Glossary
from .llm_client import LLMCallError, call_llm_async
from .models import SubtitleBlock
from .prompts import (
    CONSISTENCY_AUDIT_SYSTEM_PROMPT,
    DEFAULT_STYLE_GUIDE,
    TIMELINE_REVIEW_SYSTEM_PROMPT,
)
from .streaming import (
    append_block_spool,
    append_ndjson,
    build_block_spool,
    count_spool_lines,
    iter_block_spool,
    iter_block_windows,
    iter_chunks_with_context,
    recover_block_spool,
    save_blocks_as_srt,
)
from .text_utils import clean_translated_text, validate_translation
from .translator import translate_chunk_task

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int], Awaitable[None]]
LogCallback = Callable[[str, str, bool], Awaitable[None]]
PIPELINE_SCHEMA_VERSION = 3
REPORT_DETAIL_SAMPLE_LIMIT = 500


class ResumeMismatchError(RuntimeError):
    """Raised when persisted work cannot safely be resumed."""


class TranslationQualityError(RuntimeError):
    """Raised when a run finishes without enough translated material."""


@dataclass
class TranslationStageStats:
    """Actual outcomes from the translation stage."""

    processed_count: int = 0
    translated_count: int = 0
    fallback_original_count: int = 0

    @property
    def successful_ratio(self) -> float:
        if not self.processed_count:
            return 0.0
        return self.translated_count / self.processed_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed_count": self.processed_count,
            "translated_count": self.translated_count,
            "fallback_original_count": self.fallback_original_count,
            "successful_ratio": round(self.successful_ratio, 4),
        }


@dataclass
class StreamingAgentResult:
    """Result of a streaming agent translation run."""

    output_path: Path
    report_path: Path | None
    block_count: int
    translated_count: int
    fallback_original_count: int = 0
    status: str = "completed"
    successful_ratio: float = 1.0


def _source_text(block: SubtitleBlock) -> str:
    return " ".join(entry.text for entry in block.source_entries) or block.entry.text


def _fallback_analysis(window_id: int, blocks: list[SubtitleBlock]) -> AgentWindowAnalysis:
    return AgentWindowAnalysis(
        window_id=window_id,
        block_start=blocks[0].block_id,
        block_end=blocks[-1].block_id,
        summary=" / ".join(block.entry.text for block in blocks[:5])[:500],
        style=DEFAULT_STYLE_GUIDE,
        risks=["窗口分析失败，已使用保守回退策略"],
    )


def _fallback_plan(analyses: Iterable[AgentWindowAnalysis], glossary: Glossary) -> AgentPlan:
    records = list(analyses)
    terms = _merge_window_terms(records, glossary.to_dict())
    voices: dict[str, str] = {}
    for analysis in records:
        voices.update(analysis.characters)
    return AgentPlan(
        style_guide=DEFAULT_STYLE_GUIDE,
        glossary=terms,
        character_voices=voices,
        window_summaries=[
            {
                "window_id": analysis.window_id,
                "block_start": analysis.block_start,
                "block_end": analysis.block_end,
                "summary": analysis.summary,
                "characters": analysis.characters,
                "risks": analysis.risks,
            }
            for analysis in records
        ],
        risks=[risk for analysis in records for risk in analysis.risks][:80],
    )


class StreamingAgentPipeline:
    """Streaming, recoverable, quality-first subtitle translation pipeline."""

    def __init__(self, config: TranslatorConfig):
        self.config = config
        self._translation_memory: Dict[str, str] = {}
        self._sem = asyncio.Semaphore(1)
        self._detail_paths: dict[str, Path] = {}

    async def _progress(
        self,
        callback: ProgressCallback | None,
        stage: str,
        pct: int,
    ) -> None:
        if callback:
            await callback(stage, max(0, min(100, pct)))

    async def _log(
        self,
        callback: LogCallback | None,
        stage: str,
        message: str,
        is_error: bool = False,
    ) -> None:
        if callback:
            await callback(stage, message, is_error)
        if is_error:
            logger.warning("[%s] %s", stage, message)
        else:
            logger.info("[%s] %s", stage, message)

    def _spool_paths(self, spool_dir: Path) -> dict[str, Path]:
        return {
            "manifest": spool_dir / "run-manifest.json",
            "checkpoint": spool_dir / "checkpoint.json",
            "blocks": spool_dir / "blocks.ndjson",
            "analyses": spool_dir / "window-analyses.ndjson",
            "plan": spool_dir / "agent-plan.json",
            "translated": spool_dir / "translated.ndjson",
            "reviewed": spool_dir / "reviewed.ndjson",
            "audited": spool_dir / "audited.ndjson",
            "failures": spool_dir / "failure-events.ndjson",
            "retries": spool_dir / "retry-events.ndjson",
        }

    async def run_file(
        self,
        input_path: Path,
        output_path: Path,
        report_path: Path | None,
        glossary: Glossary,
        client: Any,
        summary_prompt: str | None = None,
        translation_prompt: str | None = None,
        merged_output_path: Path | None = None,
        spool_dir: Path | None = None,
        resume: bool = False,
        on_progress: ProgressCallback | None = None,
        on_log: LogCallback | None = None,
    ) -> StreamingAgentResult:
        """Run the streaming agent pipeline from an SRT file to output files."""
        spool_dir = spool_dir or input_path.with_suffix(input_path.suffix + ".agent-spool")
        spool_dir.mkdir(parents=True, exist_ok=True)
        paths = self._spool_paths(spool_dir)
        report = AgentReport(str(input_path), str(output_path))

        try:
            self._prepare_run(
                paths,
                input_path,
                glossary,
                summary_prompt,
                translation_prompt,
                resume,
            )
            self._configure_detail_events(paths, report, resume)

            await self._progress(on_progress, "reading", 1)
            block_count = await self._build_or_reuse_blocks(
                input_path, paths["blocks"], resume, on_log
            )
            report.stats["block_count"] = block_count
            self._write_checkpoint(paths["checkpoint"], "reading", block_count=block_count)
            if merged_output_path:
                save_blocks_as_srt(iter_block_spool(paths["blocks"]), merged_output_path)
            await self._progress(on_progress, "reading", 100)

            await self._progress(on_progress, "planning", 0)
            plan = await self._build_or_reuse_plan(
                paths,
                glossary,
                client,
                summary_prompt,
                resume,
                on_progress,
                on_log,
            )
            report.window_analyses = list(plan.window_summaries)
            report.agent_plan = plan.to_dict()
            report.risks = list(plan.risks)

            await self._progress(on_progress, "translating", 0)
            translation_stats = await self._translate_blocks(
                paths["blocks"],
                paths["translated"],
                paths["checkpoint"],
                plan,
                client,
                translation_prompt,
                resume,
                report,
                on_progress,
                on_log,
            )
            report.stats.update(translation_stats.to_dict())
            if translation_stats.translated_count == 0:
                raise TranslationQualityError(
                    "没有成功翻译任何字幕。请检查 API、模型配置或字幕内容。"
                )
            if translation_stats.successful_ratio < self.config.minimum_success_ratio:
                raise TranslationQualityError(
                    "成功译文比例过低 "
                    f"({translation_stats.successful_ratio:.1%})，已停止输出以避免误用。"
                )

            await self._progress(on_progress, "reviewing", 0)
            reviewed_count = await self._review_timeline(
                paths["translated"],
                paths["reviewed"],
                client,
                report,
                on_progress,
                on_log,
            )
            report.stats["reviewed_count"] = reviewed_count
            self._write_checkpoint(paths["checkpoint"], "reviewing", count=reviewed_count)

            await self._progress(on_progress, "auditing", 0)
            audited_count = await self._audit_consistency(
                paths["reviewed"],
                paths["audited"],
                plan,
                client,
                report,
                on_progress,
                on_log,
            )
            report.stats["audited_count"] = audited_count
            self._write_checkpoint(paths["checkpoint"], "auditing", count=audited_count)

            await self._progress(on_progress, "writing", 0)
            save_blocks_as_srt(iter_block_spool(paths["audited"]), output_path)
            report.status = (
                "completed_with_warnings"
                if translation_stats.fallback_original_count
                else "completed"
            )
            report.completed_at = datetime.now().isoformat()
            if report_path and self.config.report_enabled:
                self._persist_detail_files(report, report_path)
                report.save(report_path)
                await self._log(on_log, "writing", f"Agent report saved: {report_path}")
            await self._progress(on_progress, "writing", 100)

            return StreamingAgentResult(
                output_path=output_path,
                report_path=report_path if self.config.report_enabled else None,
                block_count=block_count,
                translated_count=translation_stats.translated_count,
                fallback_original_count=translation_stats.fallback_original_count,
                status=report.status,
                successful_ratio=translation_stats.successful_ratio,
            )
        except Exception as exc:
            report.status = "error"
            report.error = str(exc)
            report.completed_at = datetime.now().isoformat()
            if isinstance(exc, LLMCallError):
                report.stats.setdefault("failed_request_count", 1)
            if report_path and self.config.report_enabled:
                try:
                    self._persist_detail_files(report, report_path)
                    report.save(report_path)
                except Exception as report_exc:
                    logger.warning("Failed to save error report: %s", report_exc)
            raise

    def _prepare_run(
        self,
        paths: dict[str, Path],
        input_path: Path,
        glossary: Glossary,
        summary_prompt: str | None,
        translation_prompt: str | None,
        resume: bool,
    ) -> None:
        expected = self._manifest_payload(
            input_path, glossary, summary_prompt, translation_prompt
        )
        manifest_path = paths["manifest"]
        if resume:
            if not manifest_path.exists():
                raise ResumeMismatchError(
                    "无法恢复：未找到运行清单。请移除 --resume 重新开始。"
                )
            stored = json.loads(manifest_path.read_text(encoding="utf-8"))
            if stored != expected:
                raise ResumeMismatchError(
                    "无法恢复：输入文件、术语表、提示词、模型或处理参数已变化。"
                )
            return

        for name, path in paths.items():
            if name != "manifest" and path.exists():
                path.unlink()
        self._write_json_atomic(manifest_path, expected)

    def _configure_detail_events(
        self,
        paths: dict[str, Path],
        report: AgentReport,
        resume: bool,
    ) -> None:
        self._detail_paths = {"failures": paths["failures"], "retries": paths["retries"]}
        if resume:
            if paths["failures"].exists():
                paths["failures"].unlink()
            report.stats["retry_count"] = count_spool_lines(paths["retries"])
        else:
            report.stats["retry_count"] = 0
        report.stats["failure_count"] = 0

    def _record_detail(
        self,
        report: AgentReport,
        category: str,
        payload: dict[str, Any],
    ) -> None:
        path = self._detail_paths.get(category)
        if path:
            append_ndjson(path, payload)
        count_key = "failure_count" if category == "failures" else "retry_count"
        report.stats[count_key] = report.stats.get(count_key, 0) + 1
        records = getattr(report, category)
        if len(records) < REPORT_DETAIL_SAMPLE_LIMIT:
            records.append(payload)
        else:
            report.stats[f"{category}_sample_truncated"] = True

    def _persist_detail_files(self, report: AgentReport, report_path: Path) -> None:
        for category, source in self._detail_paths.items():
            count_key = "failure_count" if category == "failures" else "retry_count"
            records = getattr(report, category)
            if report.stats.get(count_key, 0) <= len(records) or not source.exists():
                continue
            destination = report_path.with_suffix(report_path.suffix + f".{category}.ndjson")
            shutil.copyfile(source, destination)
            report.detail_files[category] = str(destination)

    def _manifest_payload(
        self,
        input_path: Path,
        glossary: Glossary,
        summary_prompt: str | None,
        translation_prompt: str | None,
    ) -> dict[str, Any]:
        config_keys = (
            "model_name",
            "summary_model_name",
            "max_output_tokens",
            "chunk_size",
            "context_window",
            "analysis_window_entries",
            "analysis_window_max_chars",
            "repair_attempts",
            "max_chars_per_entry",
            "merge_time_gap",
            "minimum_success_ratio",
        )
        return {
            "schema_version": PIPELINE_SCHEMA_VERSION,
            "input_sha256": self._file_sha256(input_path),
            "input_size": input_path.stat().st_size,
            "config": {key: getattr(self.config, key) for key in config_keys},
            "glossary_sha256": self._value_sha256(glossary.to_dict()),
            "summary_prompt_sha256": self._value_sha256(summary_prompt or ""),
            "translation_prompt_sha256": self._value_sha256(translation_prompt or ""),
        }

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _value_sha256(value: Any) -> str:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def _write_checkpoint(self, path: Path, stage: str, **values: Any) -> None:
        self._write_json_atomic(
            path,
            {"stage": stage, "updated_at": datetime.now().isoformat(), **values},
        )

    async def _build_or_reuse_blocks(
        self,
        input_path: Path,
        block_path: Path,
        resume: bool,
        on_log: LogCallback | None,
    ) -> int:
        if resume and block_path.exists():
            count = recover_block_spool(block_path)
            if count > 0:
                await self._log(on_log, "reading", f"Reusing {count} subtitle blocks")
                return count

        if block_path.exists():
            block_path.unlink()
        await self._log(on_log, "reading", "Streaming SRT and smart-merging English subtitles")
        count = await asyncio.to_thread(
            build_block_spool,
            input_path,
            block_path,
            self.config.max_chars_per_entry,
            self.config.merge_time_gap,
        )
        if count == 0:
            raise ValueError("字幕文件为空或格式不正确。")
        await self._log(on_log, "reading", f"Prepared {count} merged subtitle blocks")
        return count

    def _iter_analyses(self, path: Path) -> Iterator[AgentWindowAnalysis]:
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    yield AgentWindowAnalysis.from_dict(json.loads(line))

    async def _build_or_reuse_plan(
        self,
        paths: dict[str, Path],
        glossary: Glossary,
        client: Any,
        summary_prompt: str | None,
        resume: bool,
        on_progress: ProgressCallback | None,
        on_log: LogCallback | None,
    ) -> AgentPlan:
        if resume and paths["plan"].exists():
            try:
                plan = AgentPlan.from_dict(
                    json.loads(paths["plan"].read_text(encoding="utf-8"))
                )
                await self._log(on_log, "planning", "Reusing existing AgentPlan")
                await self._progress(on_progress, "planning", 100)
                return plan
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                await self._log(
                    on_log,
                    "planning",
                    "Existing AgentPlan is incomplete; rebuilding it",
                    is_error=True,
                )

        for path in (paths["analyses"], paths["plan"]):
            if path.exists():
                path.unlink()

        total_windows = sum(
            1
            for _ in iter_block_windows(
                paths["blocks"],
                self.config.analysis_window_entries,
                self.config.analysis_window_max_chars,
            )
        )
        total = max(1, total_windows)
        for idx, blocks in enumerate(
            iter_block_windows(
                paths["blocks"],
                self.config.analysis_window_entries,
                self.config.analysis_window_max_chars,
            )
        ):
            try:
                analysis = await analyze_window(
                    client,
                    self.config.summary_model_name,
                    idx,
                    blocks,
                    self.config,
                    custom_prompt=summary_prompt,
                )
            except LLMCallError:
                raise
            except Exception as exc:
                await self._log(
                    on_log,
                    "planning",
                    f"Window {idx} analysis failed, using fallback: {exc}",
                    is_error=True,
                )
                analysis = _fallback_analysis(idx, blocks)
            append_ndjson(paths["analyses"], analysis.to_dict())
            await self._progress(on_progress, "planning", int(((idx + 1) / total) * 70))

        try:
            plan = await reduce_agent_plan(
                client,
                self.config.summary_model_name,
                self._iter_analyses(paths["analyses"]),
                glossary,
                self.config,
            )
        except LLMCallError:
            raise
        except Exception as exc:
            await self._log(
                on_log,
                "planning",
                f"AgentPlan reduce failed, using fallback: {exc}",
                is_error=True,
            )
            plan = _fallback_plan(self._iter_analyses(paths["analyses"]), glossary)

        self._write_json_atomic(paths["plan"], plan.to_dict())
        await self._log(on_log, "planning", "AgentPlan generated")
        await self._progress(on_progress, "planning", 100)
        return plan

    async def _translate_blocks(
        self,
        block_path: Path,
        translated_path: Path,
        checkpoint_path: Path,
        plan: AgentPlan,
        client: Any,
        translation_prompt: str | None,
        resume: bool,
        report: AgentReport,
        on_progress: ProgressCallback | None,
        on_log: LogCallback | None,
    ) -> TranslationStageStats:
        block_count = count_spool_lines(block_path)
        if resume:
            recover_block_spool(translated_path, stop_before_failed=True)
        elif translated_path.exists():
            translated_path.unlink()
        stats = self._translation_spool_stats(translated_path)
        if resume and stats.processed_count:
            self._rebuild_memory_from_spool(translated_path)
            await self._log(
                on_log, "translating", f"Resuming after {stats.processed_count} blocks"
            )

        for chunk, prev_context, next_context in iter_chunks_with_context(
            block_path,
            self.config.chunk_size,
            self.config.context_window,
        ):
            pending = [block for block in chunk if block.block_id >= stats.processed_count]
            if not pending:
                continue
            translated_blocks = await self._translate_with_repair(
                pending,
                prev_context,
                next_context,
                plan,
                client,
                translation_prompt,
                report,
                self.config.repair_attempts,
            )
            for block in translated_blocks:
                failed = block.stage == "failed"
                saved = block.copy(stage="failed" if failed else "translated")
                append_block_spool(translated_path, saved)
                stats.processed_count += 1
                if failed:
                    stats.fallback_original_count += 1
                else:
                    stats.translated_count += 1
                    self._translation_memory[_source_text(saved)] = saved.entry.text
                    if len(self._translation_memory) > 300:
                        items = list(self._translation_memory.items())
                        self._translation_memory = dict(items[-150:])
                self._write_checkpoint(
                    checkpoint_path,
                    "translating",
                    last_block_id=saved.block_id,
                    **stats.to_dict(),
                )
            await self._progress(
                on_progress,
                "translating",
                int((stats.processed_count / max(1, block_count)) * 100),
            )
        return stats

    def _translation_spool_stats(self, path: Path) -> TranslationStageStats:
        stats = TranslationStageStats()
        if not path.exists():
            return stats
        for block in iter_block_spool(path):
            stats.processed_count += 1
            if block.stage == "failed":
                stats.fallback_original_count += 1
            else:
                stats.translated_count += 1
        return stats

    def _rebuild_memory_from_spool(self, translated_path: Path) -> None:
        for block in iter_block_spool(translated_path):
            if block.stage != "failed":
                self._translation_memory[_source_text(block)] = block.entry.text
        if len(self._translation_memory) > 300:
            items = list(self._translation_memory.items())
            self._translation_memory = dict(items[-150:])

    async def _translate_with_repair(
        self,
        blocks: list[SubtitleBlock],
        prev_context: list[str],
        next_context: list[str],
        plan: AgentPlan,
        client: Any,
        translation_prompt: str | None,
        report: AgentReport,
        attempts_left: int,
    ) -> list[SubtitleBlock]:
        if not blocks:
            return []

        agent_brief = plan.brief_for_block(blocks[0].block_id)
        chunk_data = [{"index": block.block_id, "text": block.entry.text} for block in blocks]
        try:
            results = await translate_chunk_task(
                client=client,
                chunk_data=chunk_data,
                context_prev=prev_context,
                context_next=next_context,
                agent_brief=agent_brief,
                glossary=plan.glossary,
                model=self.config.model_name,
                sem=self._sem,
                custom_translation_prompt=translation_prompt,
                translation_memory=self._translation_memory,
                max_tokens=self.config.max_output_tokens,
            )
        except LLMCallError:
            report.stats["failed_request_count"] = report.stats.get("failed_request_count", 0) + 1
            raise
        except Exception as exc:
            self._record_detail(
                report,
                "retries",
                {
                    "block_start": blocks[0].block_id,
                    "block_end": blocks[-1].block_id,
                    "size": len(blocks),
                    "reason": str(exc),
                },
            )
            if attempts_left > 1:
                if len(blocks) == 1:
                    return await self._translate_with_repair(
                        blocks,
                        prev_context,
                        next_context,
                        plan,
                        client,
                        translation_prompt,
                        report,
                        attempts_left - 1,
                    )
                mid = max(1, len(blocks) // 2)
                left = await self._translate_with_repair(
                    blocks[:mid],
                    prev_context,
                    [block.entry.text for block in blocks[mid:mid + self.config.context_window]],
                    plan,
                    client,
                    translation_prompt,
                    report,
                    attempts_left - 1,
                )
                right = await self._translate_with_repair(
                    blocks[mid:],
                    [block.entry.text for block in blocks[max(0, mid - self.config.context_window):mid]],
                    next_context,
                    plan,
                    client,
                    translation_prompt,
                    report,
                    attempts_left - 1,
                )
                return [*left, *right]
            return [self._failed_block(block, report, str(exc)) for block in blocks]

        by_index = {result.index: result for result in results}
        translated: list[SubtitleBlock] = []
        failed: list[SubtitleBlock] = []
        for block in blocks:
            result = by_index.get(block.block_id)
            if result and result.success:
                text = clean_translated_text(result.translated)
                translated.append(block.copy(entry=block.entry.copy(text=text)))
            else:
                failed.append(block)

        if failed and attempts_left > 1:
            repaired = await self._translate_with_repair(
                failed,
                prev_context,
                next_context,
                plan,
                client,
                translation_prompt,
                report,
                attempts_left - 1,
            )
            translated.extend(repaired)

        translated_by_id = {block.block_id: block for block in translated}
        final_blocks: list[SubtitleBlock] = []
        for block in blocks:
            translated_block = translated_by_id.get(block.block_id)
            if translated_block is not None:
                final_blocks.append(translated_block)
            else:
                final_blocks.append(
                    self._failed_block(block, report, "Translation validation failed")
                )
        return final_blocks

    def _failed_block(self, block: SubtitleBlock, report: AgentReport, reason: str) -> SubtitleBlock:
        self._record_detail(
            report,
            "failures",
            {
                "block_id": block.block_id,
                "source_start_index": block.source_start_index,
                "source_end_index": block.source_end_index,
                "reason": reason,
                "text": block.entry.text,
            },
        )
        return block.copy(stage="failed")

    async def _review_timeline(
        self,
        translated_path: Path,
        reviewed_path: Path,
        client: Any,
        report: AgentReport,
        on_progress: ProgressCallback | None,
        on_log: LogCallback | None,
    ) -> int:
        if reviewed_path.exists():
            reviewed_path.unlink()
        chunk_size = max(1, min(self.config.chunk_size, 20))
        total = max(1, (count_spool_lines(translated_path) + chunk_size - 1) // chunk_size)
        for idx, chunk in enumerate(self._iter_spool_chunks(translated_path, chunk_size)):
            active = [block for block in chunk if block.stage != "failed"]
            reviewed_active = await self._review_chunk(active, client, report) if active else []
            by_id = {block.block_id: block for block in reviewed_active}
            for block in chunk:
                output = by_id.get(block.block_id, block)
                stage = "failed" if output.stage == "failed" else "reviewed"
                append_block_spool(reviewed_path, output.copy(stage=stage))
            await self._progress(on_progress, "reviewing", int(((idx + 1) / total) * 100))
        await self._log(on_log, "reviewing", "Timeline review complete")
        return count_spool_lines(reviewed_path)

    async def _review_chunk(
        self,
        blocks: list[SubtitleBlock],
        client: Any,
        report: AgentReport,
    ) -> list[SubtitleBlock]:
        items = [
            {
                "id": idx,
                "timecode": block.entry.timecode,
                "original_timecodes": [entry.timecode for entry in block.source_entries],
                "original": _source_text(block),
                "translated": block.entry.text,
            }
            for idx, block in enumerate(blocks)
        ]
        system_prompt = TIMELINE_REVIEW_SYSTEM_PROMPT
        user_prompt = f"""请质检以下字幕块：
{json.dumps(items, ensure_ascii=False)}

只输出 JSON："""
        try:
            raw = await call_llm_async(
                client,
                self.config.model_name,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_retries=2,
                json_mode=True,
                max_tokens=self.config.max_output_tokens,
            )
            corrections = _parse_json_object(raw).get("corrections", [])
        except LLMCallError:
            raise
        except Exception as exc:
            report.risks.append(f"Timeline review chunk failed: {exc}")
            return blocks
        return self._apply_corrections(blocks, corrections, report, "review")

    def _build_audit_index(
        self,
        reviewed_path: Path,
        plan: AgentPlan,
        index_path: Path,
    ) -> int:
        """Index consistency targets on disk so long files do not grow RAM use."""
        if index_path.exists():
            index_path.unlink()
        terms = [term.casefold() for term in plan.glossary if term.strip()]
        people = [name.casefold() for name in plan.character_voices if name.strip()]
        risky_ranges = [
            (int(summary["block_start"]), int(summary["block_end"]))
            for summary in plan.window_summaries
            if summary.get("risks")
        ]

        with closing(sqlite3.connect(index_path)) as database:
            with database:
                database.execute(
                    "CREATE TABLE seen (source TEXT PRIMARY KEY, block_id INTEGER, translated TEXT)"
                )
                database.execute(
                    "CREATE TABLE targets (block_id INTEGER PRIMARY KEY, reasons TEXT NOT NULL)"
                )

                def mark(block_id: int, reason: str) -> None:
                    row = database.execute(
                        "SELECT reasons FROM targets WHERE block_id = ?", (block_id,)
                    ).fetchone()
                    reasons = set(json.loads(row[0])) if row else set()
                    reasons.add(reason)
                    database.execute(
                        "INSERT OR REPLACE INTO targets (block_id, reasons) VALUES (?, ?)",
                        (block_id, json.dumps(sorted(reasons), ensure_ascii=False)),
                    )

                for block in iter_block_spool(reviewed_path):
                    if block.stage == "failed":
                        continue
                    source = _source_text(block).casefold()
                    translated = block.entry.text.strip()
                    if any(term in source for term in terms):
                        mark(block.block_id, "术语表命中")
                    if any(person in source or person in translated.casefold() for person in people):
                        mark(block.block_id, "人物称呼命中")
                    if any(start <= block.block_id <= end for start, end in risky_ranges):
                        mark(block.block_id, "分析风险窗口")
                    repeated = database.execute(
                        "SELECT block_id, translated FROM seen WHERE source = ?", (source,)
                    ).fetchone()
                    if repeated and repeated[1] != translated:
                        mark(int(repeated[0]), "重复短语译法不一致")
                        mark(block.block_id, "重复短语译法不一致")
                    elif not repeated:
                        database.execute(
                            "INSERT INTO seen (source, block_id, translated) VALUES (?, ?, ?)",
                            (source, block.block_id, translated),
                        )
                row = database.execute("SELECT COUNT(*) FROM targets").fetchone()
                return int(row[0]) if row else 0

    @staticmethod
    def _load_audit_flags(
        database: sqlite3.Connection,
        blocks: list[SubtitleBlock],
    ) -> dict[int, set[str]]:
        if not blocks:
            return {}
        ids = [block.block_id for block in blocks]
        placeholders = ",".join("?" for _ in ids)
        rows = database.execute(
            f"SELECT block_id, reasons FROM targets WHERE block_id IN ({placeholders})",
            ids,
        ).fetchall()
        return {int(block_id): set(json.loads(reasons)) for block_id, reasons in rows}

    async def _audit_consistency(
        self,
        reviewed_path: Path,
        audited_path: Path,
        plan: AgentPlan,
        client: Any,
        report: AgentReport,
        on_progress: ProgressCallback | None,
        on_log: LogCallback | None,
    ) -> int:
        if audited_path.exists():
            audited_path.unlink()
        audit_index_path = audited_path.with_suffix(audited_path.suffix + ".sqlite3")
        flagged_count = self._build_audit_index(reviewed_path, plan, audit_index_path)
        chunk_size = max(1, min(self.config.chunk_size, 20))
        total = max(1, (count_spool_lines(reviewed_path) + chunk_size - 1) // chunk_size)
        correction_count = 0
        target_sample: list[dict[str, Any]] = []
        try:
            with closing(sqlite3.connect(audit_index_path)) as database:
                for idx, chunk in enumerate(self._iter_spool_chunks(reviewed_path, chunk_size)):
                    flags = self._load_audit_flags(database, chunk)
                    for block_id, reasons in flags.items():
                        if len(target_sample) < 100:
                            target_sample.append(
                                {"block_id": block_id, "reasons": sorted(reasons)}
                            )
                    selected = [block for block in chunk if block.block_id in flags]
                    audited_selected = (
                        await self._audit_chunk(selected, plan, client, report, flags)
                        if selected
                        else []
                    )
                    by_id = {block.block_id: block for block in audited_selected}
                    for before in chunk:
                        after = by_id.get(before.block_id, before)
                        if before.entry.text != after.entry.text:
                            correction_count += 1
                        stage = "failed" if after.stage == "failed" else "audited"
                        append_block_spool(audited_path, after.copy(stage=stage))
                    await self._progress(on_progress, "auditing", int(((idx + 1) / total) * 100))
        finally:
            audit_index_path.unlink(missing_ok=True)
        report.audit = {
            "flagged_blocks": flagged_count,
            "checked_blocks": flagged_count,
            "corrections": correction_count,
            "glossary_terms": len(plan.glossary),
            "character_rules": len(plan.character_voices),
            "target_sample": target_sample,
        }
        await self._log(on_log, "auditing", f"Consistency audit complete, corrections: {correction_count}")
        return count_spool_lines(audited_path)

    async def _audit_chunk(
        self,
        blocks: list[SubtitleBlock],
        plan: AgentPlan,
        client: Any,
        report: AgentReport,
        flags: dict[int, set[str]] | None = None,
    ) -> list[SubtitleBlock]:
        terms = "\n".join(
            f"- {source} -> {target}"
            for source, target in list(plan.glossary.items())[:120]
        )
        voices = "\n".join(
            f"- {name}: {voice}"
            for name, voice in list(plan.character_voices.items())[:80]
        )
        items = [
            {
                "id": idx,
                "block_id": block.block_id,
                "reason": sorted((flags or {}).get(block.block_id, set())),
                "original": _source_text(block),
                "translated": block.entry.text,
            }
            for idx, block in enumerate(blocks)
        ]
        system_prompt = CONSISTENCY_AUDIT_SYSTEM_PROMPT
        user_prompt = f"""术语表：
{terms or "无"}

人物称呼规则：
{voices or "无"}

被标记字幕块：
{json.dumps(items, ensure_ascii=False)}

只输出 JSON："""
        try:
            raw = await call_llm_async(
                client,
                self.config.model_name,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_retries=2,
                json_mode=True,
                max_tokens=self.config.max_output_tokens,
            )
            corrections = _parse_json_object(raw).get("corrections", [])
        except LLMCallError:
            raise
        except Exception as exc:
            report.risks.append(f"Consistency audit chunk failed: {exc}")
            return blocks
        return self._apply_corrections(blocks, corrections, report, "audit")

    def _apply_corrections(
        self,
        blocks: list[SubtitleBlock],
        corrections: Any,
        report: AgentReport,
        stage: str,
    ) -> list[SubtitleBlock]:
        if not isinstance(corrections, list):
            return blocks
        corrected_by_id: dict[int, str] = {}
        for item in corrections:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            text = item.get("text") or item.get("corrected_text")
            if isinstance(item_id, int) and isinstance(text, str):
                corrected_by_id[item_id] = clean_translated_text(text)

        result: list[SubtitleBlock] = []
        for idx, block in enumerate(blocks):
            corrected = corrected_by_id.get(idx)
            if corrected:
                valid, reason = validate_translation(_source_text(block), corrected)
                if valid:
                    result.append(block.copy(entry=block.entry.copy(text=corrected)))
                else:
                    report.risks.append(
                        f"{stage} correction rejected for block {block.block_id}: {reason}"
                    )
                    result.append(block)
            else:
                result.append(block)
        return result

    def _iter_spool_chunks(self, path: Path, chunk_size: int) -> Iterator[list[SubtitleBlock]]:
        current: list[SubtitleBlock] = []
        for block in iter_block_spool(path):
            current.append(block)
            if len(current) >= chunk_size:
                yield current
                current = []
        if current:
            yield current


def cleanup_spool_dir(spool_dir: Path) -> None:
    """Remove a spool directory if it exists."""
    if spool_dir.exists():
        shutil.rmtree(spool_dir, ignore_errors=True)
