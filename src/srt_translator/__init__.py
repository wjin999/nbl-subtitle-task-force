"""
NBL Subtitle Task Force - Agent-powered subtitle translation.

Features:
- English subtitle merging using spaCy NLP
- Agent brief, draft translation, and review turns with LLM
- Glossary support for consistent terminology
- Streaming Agent processing for long SRT files
- Manifest-validated resume support
"""

__version__ = "2.0.0"
__author__ = "wjin999"

from .models import SrtEntry, SubtitleBlock
from .parser import parse_srt, save_srt, save_srt_iter, iter_srt_entries, validate_srt_file
from .merger import merge_entries, merge_entries_batch, init_spacy_model
from .translator import (
    translate_chunk_task,
    generate_agent_brief,
    generate_context_summary,
    TranslationResult,
)
from .glossary import load_glossary, load_glossary_from_string, Glossary, find_matching_terms
from .text_utils import clean_translated_text, validate_translation
from .config import TranslatorConfig
from .streaming_pipeline import StreamingAgentPipeline, StreamingAgentResult
from .agent_plan import AgentPlan, AgentWindowAnalysis, AgentReport

__all__ = [
    # Models
    "SrtEntry",
    "SubtitleBlock",
    "TranslationResult",
    "AgentPlan",
    "AgentWindowAnalysis",
    "AgentReport",
    "StreamingAgentResult",
    "TranslatorConfig",
    "Glossary",
    # Parsing
    "parse_srt",
    "save_srt",
    "save_srt_iter",
    "iter_srt_entries",
    "validate_srt_file",
    # Merging
    "merge_entries",
    "merge_entries_batch",
    "init_spacy_model",
    # Translation
    "translate_chunk_task",
    "generate_agent_brief",
    "generate_context_summary",
    # Pipeline
    "StreamingAgentPipeline",
    # Glossary
    "load_glossary",
    "load_glossary_from_string",
    "find_matching_terms",
    # Utils
    "clean_translated_text",
    "validate_translation",
]
