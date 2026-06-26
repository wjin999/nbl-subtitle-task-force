"""Configuration and constants."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Load environment variables once
load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class TranslatorConfig:
    """Configuration for subtitle translator."""
    
    # API settings
    api_key: Optional[str] = None
    model_name: str = "deepseek-v4-pro"          # 默认模型，可通过 DEEPSEEK_MODEL 环境变量覆盖
    summary_model_name: str = "deepseek-v4-pro"  # 默认摘要模型，可通过 DEEPSEEK_SUMMARY_MODEL 覆盖
    max_output_tokens: int = 4096
    request_timeout: float = 60.0
    
    # Agent processing settings
    chunk_size: int = 10
    context_window: int = 7
    analysis_window_entries: int = 120
    analysis_window_max_chars: int = 9000
    repair_attempts: int = 3
    minimum_success_ratio: float = 0.5
    
    # English smart merge settings (spaCy smart merging is always enabled)
    max_chars_per_entry: int = 300
    merge_time_gap: float = 1.5
    
    # Output settings
    output_prefix: str = "translated_"
    report_enabled: bool = True
    report_suffix: str = ".agent-report.json"
    
    # Deprecated model names
    DEPRECATED_MODELS = {"deepseek-reasoner"}
    
    def __post_init__(self):
        """Load API key from environment if not provided."""
        if self.api_key is None:
            self.api_key = self._default_api_key()

    def _default_api_key(self) -> Optional[str]:
        """Read the DeepSeek API key from environment variables."""
        return os.environ.get("DEEPSEEK_API_KEY")
    
    @classmethod
    def from_args(cls, args) -> "TranslatorConfig":
        """Create config from argparse namespace.

        Precedence: CLI arg > env var > default ("deepseek-v4-pro").
        """
        api_key = getattr(args, 'api_key', None)
        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY")

        model_name = getattr(args, 'model_name', None)
        if model_name is None:
            model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

        summary_model_name = getattr(args, 'summary_model_name', None)
        if summary_model_name is None:
            summary_model_name = os.environ.get("DEEPSEEK_SUMMARY_MODEL", "deepseek-v4-pro")

        minimum_success_ratio = getattr(args, 'minimum_success_ratio', 0.5)
        if not isinstance(minimum_success_ratio, (int, float)):
            minimum_success_ratio = 0.5

        return cls(
            api_key=api_key,
            model_name=model_name,
            summary_model_name=summary_model_name,
            max_output_tokens=getattr(args, 'max_output_tokens', 4096),
            request_timeout=getattr(args, 'request_timeout', 60.0),
            chunk_size=getattr(args, 'chunk_size_for_translation', 10),
            context_window=getattr(args, 'context_window', 7),
            analysis_window_entries=getattr(args, 'analysis_window_entries', 120),
            analysis_window_max_chars=getattr(args, 'analysis_window_max_chars', 9000),
            repair_attempts=getattr(args, 'repair_attempts', 3),
            minimum_success_ratio=minimum_success_ratio,
            max_chars_per_entry=getattr(args, 'max_chars_per_entry', 300),
            merge_time_gap=getattr(args, 'merge_time_gap', 1.5),
        )
    
    def validate(self) -> Optional[str]:
        """
        Validate configuration.
        
        Returns:
            Error message if invalid, None if valid
        """
        if not self.api_key:
            return "API key is required. Set DEEPSEEK_API_KEY or use --api-key"

        if not self.model_name:
            return "Model name is required"

        if not self.summary_model_name:
            return "Summary model name is required"
        
        if self.chunk_size < 1 or self.chunk_size > 50:
            return f"Chunk size must be 1-50, got {self.chunk_size}"

        if self.context_window < 0 or self.context_window > 100:
            return f"Context window must be 0-100, got {self.context_window}"

        if self.analysis_window_entries < 10 or self.analysis_window_entries > 1000:
            return (
                "Analysis window entries must be 10-1000, "
                f"got {self.analysis_window_entries}"
            )

        if self.analysis_window_max_chars < 1000 or self.analysis_window_max_chars > 50000:
            return (
                "Analysis window max chars must be 1000-50000, "
                f"got {self.analysis_window_max_chars}"
            )

        if self.repair_attempts < 1 or self.repair_attempts > 5:
            return f"Repair attempts must be 1-5, got {self.repair_attempts}"

        if not 0 < self.minimum_success_ratio <= 1:
            return (
                "Minimum success ratio must be greater than 0 and at most 1, "
                f"got {self.minimum_success_ratio}"
            )

        if self.max_output_tokens < 256 or self.max_output_tokens > 32768:
            return f"Max output tokens must be 256-32768, got {self.max_output_tokens}"

        if self.request_timeout < 5 or self.request_timeout > 600:
            return f"Request timeout must be 5-600 seconds, got {self.request_timeout}"

        # Check for deprecated models
        if self.model_name in self.DEPRECATED_MODELS:
            logger.warning(
                f"Model '{self.model_name}' is deprecated and will be removed in a future version. "
                f"Please update to a newer model."
            )
        if self.summary_model_name in self.DEPRECATED_MODELS:
            logger.warning(
                f"Summary model '{self.summary_model_name}' is deprecated and will be removed in a future version. "
                f"Please update to a newer model."
            )
        
        return None


# Default glossary filename
DEFAULT_GLOSSARY_FILENAME = "glossary.txt"

# Supported file extensions
SUPPORTED_EXTENSIONS = {".srt"}
