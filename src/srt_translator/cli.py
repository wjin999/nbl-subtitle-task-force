"""Command-line interface for SRT Translator."""

from __future__ import annotations

import asyncio
import argparse
import logging
import sys
from pathlib import Path

from tqdm import tqdm

from .config import TranslatorConfig, DEFAULT_GLOSSARY_FILENAME
from .parser import validate_srt_file
from .glossary import load_glossary, Glossary
from .llm_client import create_client
from .streaming_pipeline import StreamingAgentPipeline, cleanup_spool_dir


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="NBL Subtitle Task Force",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s video.srt                     # NBL Agent translation
  %(prog)s video.srt -o output.srt       # Specify output
  %(prog)s video.srt -g glossary.txt     # Use glossary
  %(prog)s video.srt --save-merged       # Save spaCy merged source subtitles
  %(prog)s video.srt --resume            # Resume interrupted NBL Agent translation
        """
    )
    
    # Positional arguments
    parser.add_argument("input_path", help="Input SRT file path")
    parser.add_argument(
        "positional_output_path",
        nargs="?",
        default=None,
        help="Output SRT file path",
    )
    
    # Glossary
    parser.add_argument("-g", "--glossary", dest="glossary_path", help="Glossary file path")
    
    # Processing options
    parser.add_argument("-o", "--output", dest="output_path", help="Output SRT file path")
    parser.add_argument("--save-merged", action="store_true", help="Save spaCy merged source subtitles")
    parser.add_argument("--merged-output", dest="merged_output_path", help="Output path for spaCy merged source subtitles")
    parser.add_argument("--max-chars", dest="max_chars_per_entry", type=int, default=300)
    parser.add_argument("--merge-gap", dest="merge_time_gap", type=float, default=1.5)
    
    # API options
    parser.add_argument("--api-key", help="DeepSeek API key (or set DEEPSEEK_API_KEY)")
    parser.add_argument("--model", dest="model_name", default=None)
    parser.add_argument("--summary-model", dest="summary_model_name", default=None)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--request-timeout", type=float, default=60.0)
    
    # Custom prompts
    parser.add_argument("--summary-prompt", help="自定义 Agent 分析提示词（覆盖默认）")
    parser.add_argument("--translation-prompt", help="自定义 Agent 翻译提示词（覆盖默认）")
    
    # Performance
    parser.add_argument("--chunk-size", dest="chunk_size_for_translation", type=int, default=10)
    parser.add_argument("--context-window", dest="context_window", type=int, default=7, help="上下文窗口大小，控制每个 chunk 前后的额外上下文条目数量")
    parser.add_argument(
        "--minimum-success-ratio",
        type=float,
        default=0.5,
        help=argparse.SUPPRESS,
    )
    
    # Progress
    parser.add_argument("--resume", action="store_true", help="Resume from saved Agent spool")
    
    # Misc
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    
    args = parser.parse_args()
    if args.output_path and args.positional_output_path:
        parser.error("output path specified twice; use either positional output or -o/--output")
    if args.output_path is None:
        args.output_path = args.positional_output_path
    delattr(args, "positional_output_path")
    return args


async def main_async(args: argparse.Namespace) -> int:
    """Main async workflow."""
    logger = logging.getLogger(__name__)
    config = TranslatorConfig.from_args(args)
    
    # 验证配置
    error = config.validate()
    if error:
        logger.error(error)
        return 1
    
    # 验证输入文件
    in_path = Path(args.input_path).expanduser().resolve()
    error = validate_srt_file(in_path)
    if error:
        logger.error(error)
        return 1
    
    # 加载术语表
    glossary = Glossary()
    if args.glossary_path:
        g_path = Path(args.glossary_path).expanduser().resolve()
        glossary = load_glossary(g_path)
    elif Path(DEFAULT_GLOSSARY_FILENAME).exists():
        logger.info(f"Auto-detected '{DEFAULT_GLOSSARY_FILENAME}'")
        glossary = load_glossary(Path(DEFAULT_GLOSSARY_FILENAME))
    
    # 创建 API 客户端
    client = create_client(config.api_key, timeout=config.request_timeout)

    if args.output_path:
        out_path = Path(args.output_path)
    else:
        out_path = in_path.with_name(f"{config.output_prefix}{in_path.name}")

    report_path = out_path.with_name(f"{out_path.stem}{config.report_suffix}")
    merged_out_path = None
    if args.save_merged or args.merged_output_path:
        merged_out_path = (
            Path(args.merged_output_path).expanduser().resolve()
            if args.merged_output_path
            else in_path.with_name(f"merged_{in_path.name}")
        )

    spool_dir = in_path.with_suffix(in_path.suffix + ".agent-spool")
    if not args.resume and spool_dir.exists():
        cleanup_spool_dir(spool_dir)

    pbar = tqdm(total=100, desc="Agent", unit="%")
    progress_state = {"value": 0, "stage": ""}
    stage_ranges = {
        "reading": (0, 10),
        "planning": (10, 30),
        "translating": (30, 72),
        "reviewing": (72, 84),
        "auditing": (84, 95),
        "writing": (95, 100),
    }

    async def _progress(stage: str, pct: int):
        start, end = stage_ranges.get(stage, (0, 100))
        mapped = start + int((end - start) * pct / 100)
        delta = max(0, mapped - progress_state["value"])
        if delta:
            pbar.update(delta)
        progress_state["value"] = max(progress_state["value"], mapped)
        progress_state["stage"] = stage
        pbar.set_description(f"Agent {stage} ({mapped}%)")

    async def _log(stage: str, message: str, is_error: bool = False):
        if is_error:
            logger.warning("[%s] %s", stage, message)
        else:
            logger.info("[%s] %s", stage, message)

    pipeline = StreamingAgentPipeline(config)
    try:
        result = await pipeline.run_file(
            input_path=in_path,
            output_path=out_path,
            report_path=report_path,
            glossary=glossary,
            client=client,
            summary_prompt=args.summary_prompt,
            translation_prompt=args.translation_prompt,
            merged_output_path=merged_out_path,
            spool_dir=spool_dir,
            resume=args.resume,
            on_progress=_progress,
            on_log=_log,
        )
    except Exception as exc:
        pbar.close()
        logger.error(str(exc))
        return 1
    pbar.close()

    cleanup_spool_dir(spool_dir)

    logger.info(
        "Done! %s/%s blocks translated; %s preserved as original. Saved to %s. Report: %s",
        result.translated_count,
        result.block_count,
        result.fallback_original_count,
        out_path,
        report_path,
    )
    if result.status == "completed_with_warnings":
        logger.warning(
            "Translation completed with warnings: review %s preserved original blocks in the report.",
            result.fallback_original_count,
        )
    
    return 0


def main() -> None:
    """CLI entry point."""
    args = parse_arguments()
    setup_logging(args.verbose)
    
    try:
        exit_code = asyncio.run(main_async(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        # 翻译过程中每 chunk 都会增量保存进度，因此中断时进度已自动保存
        print("\nInterrupted by user.")
        sys.exit(130)
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
