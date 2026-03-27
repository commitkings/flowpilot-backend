"""
Comprehensive logging configuration for FlowPilot.

Provides structured logging with:
- Rotating file handler (flowpilot.log)
- Separate error log (error.log)
- Console output with optional colors
- Request context tracking via context vars
- Sensitive data masking utilities
"""

import logging
import os
import re
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

# Context variables for request tracing
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
conversation_id_var: ContextVar[Optional[str]] = ContextVar("conversation_id", default=None)
run_id_var: ContextVar[Optional[str]] = ContextVar("run_id", default=None)

# Log directory and file paths
LOG_DIR = Path("logs")
MAIN_LOG_FILE = LOG_DIR / "flowpilot.log"
ERROR_LOG_FILE = LOG_DIR / "error.log"

# Configuration from environment
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))  # 10MB default
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", 5))

# Patterns for sensitive data masking
SENSITIVE_PATTERNS = [
    (re.compile(r'(api[_-]?key|apikey|secret|password|token|authorization|bearer)(["\s:=]+)([^\s",}{]+)', re.I), r'\1\2***MASKED***'),
    (re.compile(r'(account[_-]?number)(["\s:=]+)(\d{4})(\d+)(\d{2})', re.I), r'\1\2\3****\5'),
    (re.compile(r'(pin|cvv|cvc)(["\s:=]+)(\d+)', re.I), r'\1\2***'),
]


def mask_sensitive_data(message: str) -> str:
    """Mask sensitive data in log messages."""
    result = message
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class ContextAwareFormatter(logging.Formatter):
    """Formatter that includes request/conversation/run context in log messages."""

    def format(self, record: logging.LogRecord) -> str:
        # Add context to the record
        request_id = request_id_var.get()
        conversation_id = conversation_id_var.get()
        run_id = run_id_var.get()

        context_parts = []
        if request_id:
            context_parts.append(f"req:{request_id[:8]}")
        if conversation_id:
            context_parts.append(f"conv:{str(conversation_id)[:8]}")
        if run_id:
            context_parts.append(f"run:{str(run_id)[:8]}")

        record.context = f"[{' '.join(context_parts)}] " if context_parts else ""
        
        # Mask sensitive data in the message
        if record.msg:
            record.msg = mask_sensitive_data(str(record.msg))

        return super().format(record)


class ColoredFormatter(ContextAwareFormatter):
    """Formatter with ANSI color codes for console output."""

    COLORS = {
        logging.DEBUG: "\033[36m",     # Cyan
        logging.INFO: "\033[32m",      # Green
        logging.WARNING: "\033[33m",   # Yellow
        logging.ERROR: "\033[31m",     # Red
        logging.CRITICAL: "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        message = super().format(record)
        if color and sys.stdout.isatty():
            return f"{color}{message}{self.RESET}"
        return message


_logging_initialized = False


def setup_logging(
    level: Optional[str] = None,
    enable_console: bool = True,
    enable_file: bool = True,
    enable_colors: bool = True,
) -> None:
    """
    Initialize the FlowPilot logging system.

    Call this once at application startup. Configures:
    - Root logger with specified level
    - Rotating file handler for flowpilot.log
    - Separate error log for ERROR/CRITICAL
    - Console handler with optional colors

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Defaults to LOG_LEVEL env var.
        enable_console: Whether to log to stdout.
        enable_file: Whether to log to files.
        enable_colors: Whether to use colored console output.
    """
    global _logging_initialized
    if _logging_initialized:
        return

    LOG_DIR.mkdir(exist_ok=True)

    log_level = getattr(logging, level or LOG_LEVEL, logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Log format
    log_format = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(context)s%(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    if enable_file:
        # Main rotating file handler
        main_handler = RotatingFileHandler(
            MAIN_LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        main_handler.setLevel(log_level)
        main_handler.setFormatter(ContextAwareFormatter(log_format, date_format))
        root_logger.addHandler(main_handler)

        # Error-only rotating file handler
        error_handler = RotatingFileHandler(
            ERROR_LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(ContextAwareFormatter(log_format, date_format))
        root_logger.addHandler(error_handler)

    if enable_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        if enable_colors:
            console_handler.setFormatter(ColoredFormatter(log_format, date_format))
        else:
            console_handler.setFormatter(ContextAwareFormatter(log_format, date_format))
        root_logger.addHandler(console_handler)

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _logging_initialized = True
    logging.getLogger(__name__).info(
        f"Logging initialized: level={logging.getLevelName(log_level)}, "
        f"file={enable_file}, console={enable_console}"
    )


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for the given module/component.

    Usage:
        from src.utilities.logging_config import get_logger
        logger = get_logger(__name__)
        logger.info("Something happened")
    """
    return logging.getLogger(name)


# Convenience logging functions with context
def log_agent_event(
    agent_name: str,
    event: str,
    details: Optional[dict[str, Any]] = None,
    level: int = logging.INFO,
) -> None:
    """Log an agent-related event with structured format."""
    logger = get_logger(f"agent.{agent_name}")
    detail_str = f" | {details}" if details else ""
    logger.log(level, f"[{event}]{detail_str}")


def log_llm_call(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int,
    agent: Optional[str] = None,
    iteration: Optional[int] = None,
) -> None:
    """Log an LLM API call with token usage and timing."""
    agent_name = agent or "unknown"
    logger = get_logger(f"agent.{agent_name}")
    iter_str = f" iter={iteration}" if iteration is not None else ""
    logger.info(
        f"[LLM_CALL] model={model} tokens={prompt_tokens}+{completion_tokens} "
        f"duration={duration_ms}ms{iter_str}"
    )


def log_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    success: bool,
    result_preview: Optional[str] = None,
    duration_ms: Optional[int] = None,
    agent: Optional[str] = None,
) -> None:
    """Log a tool call with arguments and result."""
    agent_name = agent or "unknown"
    logger = get_logger(f"agent.{agent_name}")
    status = "OK" if success else "FAILED"
    duration_str = f" duration={duration_ms}ms" if duration_ms else ""
    # Truncate arguments for logging
    args_str = str(arguments)[:200] + "..." if len(str(arguments)) > 200 else str(arguments)
    result_str = f" result={result_preview[:100]}..." if result_preview and len(result_preview) > 100 else (f" result={result_preview}" if result_preview else "")
    logger.info(f"[TOOL_CALL] {tool_name}({args_str}) -> {status}{duration_str}{result_str}")


def log_interswitch_call(
    method: str,
    endpoint: str,
    status_code: Optional[int] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
    service: Optional[str] = None,
    request_body: Optional[Any] = None,
    response_preview: Optional[str] = None,
) -> None:
    """Log an Interswitch API call."""
    logger = get_logger("interswitch")
    service_label = service.upper() if service else "API"
    
    # Build detail string
    details = []
    if request_body:
        body_str = str(request_body)[:100]
        details.append(f"body={mask_sensitive_data(body_str)}")
    if response_preview:
        resp_str = response_preview[:100]
        details.append(f"resp={mask_sensitive_data(resp_str)}")
    detail_str = f" | {', '.join(details)}" if details else ""
    
    if error:
        logger.error(f"[{service_label}] {method} {endpoint} -> ERROR: {error}{detail_str}")
    else:
        logger.info(f"[{service_label}] {method} {endpoint} -> {status_code} ({duration_ms}ms){detail_str}")


def log_chat_message(
    role: str = None,
    content: str = None,
    conversation_id: str = None,
    message: str = None,  # Alias for content
    intent: Optional[str] = None,
    confidence: Optional[float] = None,
) -> None:
    """Log a chat message (user or assistant)."""
    logger = get_logger("chat")
    
    # Support both 'content' and 'message' parameters
    msg = content or message or ""
    
    # Truncate long messages
    preview = msg[:150] + "..." if len(msg) > 150 else msg
    preview = preview.replace("\n", " ")
    
    extra = ""
    if intent:
        extra = f" intent={intent}"
        if confidence is not None:
            extra += f" conf={confidence:.2f}"
    
    conv_label = f"[conv:{conversation_id[:8]}] " if conversation_id else ""
    logger.info(f"{conv_label}[{role.upper()}] {preview}{extra}")


def log_run_event(
    run_id: str,
    step: Optional[str],
    event: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Log a run lifecycle event."""
    logger = get_logger("orchestrator")
    step_str = f"[{step}] " if step else ""
    detail_str = f" | {details}" if details else ""
    logger.info(f"[RUN:{run_id[:8]}] {step_str}{event}{detail_str}")


def log_request(
    method: str,
    path: str,
    status_code: Optional[int] = None,
    duration_ms: Optional[int] = None,
    client_ip: Optional[str] = None,
) -> None:
    """Log an HTTP request/response."""
    logger = get_logger("http")
    ip_str = f" client={client_ip}" if client_ip else ""
    if status_code is not None:
        logger.info(f"{method} {path} -> {status_code} ({duration_ms}ms){ip_str}")
    else:
        logger.info(f"{method} {path} - Started{ip_str}")


# Legacy function for backward compatibility
def setupLogger(name: str, logLevel: int = logging.INFO) -> logging.Logger:
    """Legacy setup function. Prefer get_logger() after calling setup_logging()."""
    setup_logging(level=logging.getLevelName(logLevel))
    return get_logger(name)


# Context setters for tracing
def set_request_id(request_id: str) -> None:
    """Set the request ID for context tracking."""
    request_id_var.set(request_id)


def set_conversation_id(conversation_id: str) -> None:
    """Set the conversation ID for context tracking."""
    conversation_id_var.set(conversation_id)


def set_run_id(run_id: str) -> None:
    """Set the run ID for context tracking."""
    run_id_var.set(run_id)


def get_request_id() -> Optional[str]:
    """Get the current request ID."""
    return request_id_var.get()


def get_conversation_id() -> Optional[str]:
    """Get the current conversation ID."""
    return conversation_id_var.get()


def get_run_id() -> Optional[str]:
    """Get the current run ID."""
    return run_id_var.get()
