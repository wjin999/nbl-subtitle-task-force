"""LLM API client utilities."""

from __future__ import annotations

import asyncio
import logging
from typing import List, Dict, Optional
from enum import Enum

from openai import (
    AsyncOpenAI as DeepSeekClient,
    APIConnectionError, 
    RateLimitError,
    AuthenticationError,
    BadRequestError,
    APIStatusError,
)

logger = logging.getLogger(__name__)


class APIErrorType(Enum):
    """API 错误类型分类。"""
    RATE_LIMIT = "rate_limit"      # 429 - 可重试
    CONNECTION = "connection"       # 网络问题 - 可重试
    AUTH = "auth"                   # 401 - 不可重试
    BAD_REQUEST = "bad_request"     # 400 - 不可重试
    SERVER = "server"               # 500+ - 可重试
    UNKNOWN = "unknown"


class LLMCallError(RuntimeError):
    """Raised when an LLM API call cannot be completed after retry handling."""

    def __init__(
        self,
        message: str,
        error_type: APIErrorType = APIErrorType.UNKNOWN,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


def classify_error(error: Exception) -> tuple[APIErrorType, bool]:
    """
    分类 API 错误并判断是否可重试。
    
    Returns:
        (错误类型, 是否可重试)
    """
    if isinstance(error, RateLimitError):
        return APIErrorType.RATE_LIMIT, True
    elif isinstance(error, APIConnectionError):
        return APIErrorType.CONNECTION, True
    elif isinstance(error, AuthenticationError):
        return APIErrorType.AUTH, False
    elif isinstance(error, BadRequestError):
        return APIErrorType.BAD_REQUEST, False
    elif isinstance(error, APIStatusError):
        # 5xx 错误可重试
        if hasattr(error, 'status_code') and error.status_code >= 500:
            return APIErrorType.SERVER, True
        return APIErrorType.UNKNOWN, False
    else:
        return APIErrorType.UNKNOWN, False


async def call_llm_async(
    client: DeepSeekClient,
    model: str,
    messages: List[Dict[str, str]],
    max_retries: int = 3,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """
    Make async call to LLM API with retry logic.
    
    Args:
        client: DeepSeek client instance
        model: Model name to use
        messages: List of message dictionaries
        max_retries: Maximum retry attempts
        json_mode: Whether to request JSON response format
        max_tokens: Maximum output tokens (important for JSON mode to prevent truncation)
        
    Returns:
        Response content as string, empty string on failure
    """
    last_error: Optional[Exception] = None
    last_error_type = APIErrorType.UNKNOWN
    last_retryable = False
    
    for attempt in range(max_retries):
        try:
            params: Dict = {
                "model": model, 
                "messages": messages,
            }
            if json_mode:
                params["response_format"] = {"type": "json_object"}
            if max_tokens is not None:
                params["max_tokens"] = max_tokens

            response = await client.chat.completions.create(**params)
            content = response.choices[0].message.content
            return content.strip() if content else ""
            
        except Exception as e:
            last_error = e
            error_type, retryable = classify_error(e)
            last_error_type = error_type
            last_retryable = retryable
            
            if not retryable:
                logger.error(f"Non-retryable error ({error_type.value}): {e}")
                break
            
            # 计算退避时间
            if error_type == APIErrorType.RATE_LIMIT:
                # Rate limit 使用更长的退避时间
                delay = min(2 ** (attempt + 2), 60)  # 4, 8, 16... max 60
            else:
                delay = 2 ** (attempt + 1)  # 2, 4, 8
            
            logger.warning(
                f"Retryable error ({error_type.value}): {e}. "
                f"Retry {attempt + 1}/{max_retries} in {delay}s..."
            )
            await asyncio.sleep(delay)
    
    if last_error:
        logger.error(f"All {max_retries} retries failed. Last error: {last_error}")
        raise LLMCallError(
            f"LLM API call failed ({last_error_type.value}): {last_error}",
            error_type=last_error_type,
            retryable=last_retryable,
        ) from last_error
    
    return ""


async def call_llm_with_fallback(
    client: DeepSeekClient,
    models: List[str],
    messages: List[Dict[str, str]],
    **kwargs
) -> tuple[str, str]:
    """
    Try multiple models in sequence until one succeeds.
    
    Args:
        client: DeepSeek client
        models: List of model names to try
        messages: Messages to send
        **kwargs: Additional arguments for call_llm_async
        
    Returns:
        Tuple of (response, model_used)
    """
    for model in models:
        result = await call_llm_async(client, model, messages, **kwargs)
        if result:
            return result, model
    
    return "", ""


def create_client(
    api_key: str | None,
    base_url: str = "https://api.deepseek.com",
    timeout: float = 60.0,
) -> DeepSeekClient:
    """
    Create a client for the DeepSeek API.
    
    Args:
        api_key: API key for authentication
        base_url: API base URL
        timeout: Default timeout for requests
        
    Returns:
        Configured DeepSeek client
    """
    return DeepSeekClient(
        api_key=api_key, 
        base_url=base_url,
        timeout=timeout,
    )
