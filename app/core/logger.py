import os
import sys
import threading

from loguru import logger

from app.core.config import settings

# Global flag to control console logging
_console_logging_enabled = True
_console_logging_lock = threading.Lock()


def enable_console_logging():
    """Enable console logging output - call this when CLI aesthetics are complete."""
    global _console_logging_enabled
    with _console_logging_lock:
        _console_logging_enabled = True


def disable_console_logging():
    """Disable console logging output - call this before CLI aesthetics."""
    global _console_logging_enabled
    with _console_logging_lock:
        _console_logging_enabled = False


def console_logging_filter(record):
    """Filter that controls whether a log record should go to console based on global flag."""
    return _console_logging_enabled


def setup_logger() -> None:
    """
    Configures Loguru logger with enhanced visual formatting.
    Ensures logs don't interfere with CLI aesthetics.
    """
    # Remove default handler
    logger.remove()

    # Define log format with more visually distinct level colors
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # Enhance format with emoji indicators for different log levels
    enhanced_log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "{extra[emoji]} <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # Add console handler with enhanced format
    logger.configure(extra={"emoji": "ℹ️ "})  # Default emoji

    # Function to dynamically set emoji based on log level
    def emoji_formatter(record):
        level_emoji = {
            "TRACE": "🔍",
            "DEBUG": "🐛",
            "INFO": "ℹ️ ",
            "SUCCESS": "✅",
            "WARNING": "⚠️ ",
            "ERROR": "❌",
            "CRITICAL": "🔥",
        }
        record["extra"]["emoji"] = level_emoji.get(record["level"].name, "ℹ️ ")
        return record

    # Console handler with enhanced format - with filter to control when output appears
    logger.add(
        sys.stderr,
        format=enhanced_log_format,
        level=settings.LOG_LEVEL,
        enqueue=True,
        filter=lambda record: emoji_formatter(record)
        and console_logging_filter(record),
        colorize=True,
    )

    # File handler with standard format (no emojis for log files)
    # File logging always happens, regardless of console logging state
    os.makedirs("logs", exist_ok=True)  # Ensure logs directory exists
    logger.add(
        "logs/app.log",
        rotation="500 MB",
        retention="10 days",
        format=log_format,
        level=settings.LOG_LEVEL,
        enqueue=True,
    )

    logger.success("Logger configured successfully with enhanced formatting")
