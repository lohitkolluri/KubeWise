# -*- coding: utf-8 -*-
"""
KubeWise Advanced Logging Configuration

This module provides a robust logging system for the KubeWise application with:
1. Simplified, consistent log formatting across components
2. Log message grouping to reduce repetitive output of similar messages
3. Component-specific log levels for fine-grained control
4. Differentiated console vs file logging
5. Proper JSON serialization for structured logs
6. Visual CLI tools like progress bars for metrics display
7. Log statistics tracking to monitor and reduce log noise

Usage examples:
    # Get a component-specific logger
    logger = get_logger("collector.metrics")
    
    # Adjust log levels dynamically
    set_component_log_level("kubewise.collector.prometheus", "WARNING")
    
    # Group similar logs with longer window
    set_log_grouping_window(30.0)  # 30 second window
    
    # View log grouping statistics
    stats = get_log_group_stats()
"""
import logging
import os
from pathlib import Path
import sys
from collections import defaultdict
import time
from typing import Dict, Set, Any, Optional

from loguru import logger
from rich.logging import RichHandler

from kubewise.config import settings

# Define a simplified log format with just the essential information
# This format is more compact and easier to read while still providing necessary context
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>{name}</cyan> | <level>{message}</level>"
)

# Format for grouped logs showing count of similar messages
GROUPED_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>{name}</cyan> | <level>{message}</level> "
    "[dim](+{extra[repeat_count]} similar)[/dim]"
)

# More detailed format for file logging to aid in troubleshooting
FILE_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "{extra} | <level>{message}</level>"
)

LOG_ROTATION = "10 MB"
LOG_RETENTION = "7 days"
LOG_DIR = Path(os.getenv("KUBEWISE_LOG_DIR", "./logs"))
LOG_FILE = LOG_DIR / "kubewise.log"

# Component-specific log levels
COMPONENT_LOG_LEVELS = {
    # Core components
    "kubewise": settings.log_level.upper(),
    "kubewise.api": settings.log_level.upper(),
    "kubewise.collector": settings.log_level.upper(),
    "kubewise.models": settings.log_level.upper(),
    
    # Typically noisy components can be set to higher levels by default
    "kubewise.collector.k8s_events": "INFO",
    "kubewise.remediation.diagnosis": "INFO",
    "kubewise.remediation.engine": "INFO",
    "kubernetes_asyncio": "WARNING",
    "httpx": "WARNING",
}

# Global log cache for grouping similar messages
log_cache = {}
# Default time window to group similar logs (in seconds)
DEFAULT_GROUP_WINDOW = 5

class LogGroup:
    """Class to manage groups of similar log messages."""
    
    def __init__(self, window_seconds: float = DEFAULT_GROUP_WINDOW):
        self.message_counts: Dict[str, int] = defaultdict(int)
        self.last_logged: Dict[str, float] = {}
        self.window_seconds = window_seconds
        
    def should_log(self, key: str, level: str) -> tuple[bool, int]:
        """
        Determines if a log message should be output based on recent activity.
        
        Args:
            key: The unique identifier for this message
            level: Log level of the message
            
        Returns:
            Tuple of (should_log, repeat_count)
        """
        current_time = time.time()
        
        # Always log ERROR and CRITICAL messages
        if level in ("ERROR", "CRITICAL"):
            # Reset counter for this message
            count = self.message_counts.pop(key, 0)
            self.last_logged[key] = current_time
            return True, count
            
        # Check if we've seen this message before within our window
        if key in self.last_logged:
            elapsed = current_time - self.last_logged[key]
            
            # If within grouping window, increment count and don't log
            if elapsed < self.window_seconds:
                self.message_counts[key] += 1
                return False, 0
            
            # Otherwise, time to log again
            count = self.message_counts.pop(key, 0)
            self.last_logged[key] = current_time
            return True, count
            
        # First time seeing this message
        self.last_logged[key] = current_time
        return True, 0

# Global instance
log_grouper = LogGroup()

class InterceptHandler(logging.Handler):
    """
    Intercept standard logging messages and redirect them to Loguru.

    Ensures consistent formatting for all log messages, regardless of origin library.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging() -> None:
    """
    Configure Loguru for console and optional file logging.

    Sets up console output, file logging with rotation, and intercepts
    standard library logs for unified output.
    """
    # Remove any existing handlers
    logger.remove()

    # Configure component-specific log levels
    for component, level in COMPONENT_LOG_LEVELS.items():
        logger.level(component, level)

    # Get base log level from settings
    base_log_level = settings.log_level.upper()

    # Configure console logging via RichHandler with simplified format
    console_handler_id = logger.add(
        RichHandler(
            level=base_log_level,
            show_path=False,
            markup=True,
            rich_tracebacks=True,
            log_time_format="%Y-%m-%d %H:%M:%S",
            omit_repeated_times=True,
            enable_link_path=False,
        ),
        format=LOG_FORMAT,
        filter=lambda record: 
            not record.get("extra", {}).get("quiet", False) and 
            _log_group_filter(record),
        level=base_log_level,
    )
    
    # Add a handler for grouped logs
    grouped_handler_id = logger.add(
        RichHandler(
            level=base_log_level,
            show_path=False,
            markup=True,
            rich_tracebacks=True,
            log_time_format="%Y-%m-%d %H:%M:%S",
            omit_repeated_times=True,
            enable_link_path=False,
        ),
        format=GROUPED_LOG_FORMAT,
        filter=lambda record: 
            not record.get("extra", {}).get("quiet", False) and 
            record.get("extra", {}).get("repeat_count", 0) > 0,
        level=base_log_level,
    )

    # Configure file logging with rotation and more detailed format
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler_id = logger.add(
            LOG_FILE,
            level=base_log_level,
            format=FILE_LOG_FORMAT,
            rotation=LOG_ROTATION,
            retention=LOG_RETENTION,
            enqueue=True,
            backtrace=True,
            diagnose=True,
            colorize=False,
        )
        logger.info(f"File logging enabled: {LOG_FILE}")
    except PermissionError:
        logger.warning(
            f"Permission denied creating log directory {LOG_DIR}. File logging disabled."
        )
    except Exception as e:
        logger.error(f"Failed to set up file logging to {LOG_FILE}: {e}")

    # Intercept standard Python logging
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    
    # Log initial configuration message
    logger.info(f"KubeWise logging configured at level {base_log_level}")


def _log_group_filter(record):
    """Filter function that handles log grouping."""
    # Get message and level
    msg = record["message"]
    level = record["level"].name
    
    # Create a key for this log message
    # Include the component name and message to group similar messages from the same component
    name = record.get("extra", {}).get("name", "unknown")
    key = f"{name}:{level}:{msg}"
    
    # Check if we should log this message
    should_log, repeat_count = log_grouper.should_log(key, level)
    
    # If there are repeat counts, add them to the record
    if repeat_count > 0:
        record["extra"]["repeat_count"] = repeat_count
    
    return should_log


def get_logger(name: str) -> logger:
    """
    Get a contextualized logger for a specific component.
    
    Args:
        name: The name of the component (will be prefixed with 'kubewise.' if not already)
        
    Returns:
        A loguru logger with the component name as context
    """
    if not name.startswith("kubewise.") and name != "kubewise":
        name = f"kubewise.{name}"
        
    # Use the component-specific log level if defined
    component_level = COMPONENT_LOG_LEVELS.get(name, settings.log_level.upper())
    
    # Create a logger with the component name as context
    return logger.bind(name=name)


def set_component_log_level(component: str, level: str) -> None:
    """
    Dynamically adjust the log level for a specific component.
    
    Args:
        component: The component name (e.g., 'kubewise.collector')
        level: The log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    if not component.startswith("kubewise.") and component != "kubewise":
        component = f"kubewise.{component}"
        
    COMPONENT_LOG_LEVELS[component] = level.upper()
    logger.level(component, level.upper())
    logger.info(f"Set {component} log level to {level.upper()}")


def set_log_grouping_window(seconds: float) -> None:
    """
    Adjust the time window for grouping similar log messages.
    
    Args:
        seconds: Number of seconds to group similar messages
    """
    global log_grouper
    log_grouper.window_seconds = seconds
    logger.info(f"Set log grouping window to {seconds} seconds")


def get_log_group_stats() -> Dict[str, Any]:
    """
    Get statistics about log grouping.
    
    Returns:
        Dictionary with stats about grouped logs
    """
    global log_grouper
    
    total_groups = len(log_grouper.message_counts)
    total_grouped_messages = sum(log_grouper.message_counts.values())
    
    # Get the top 10 most grouped messages
    top_grouped = []
    for key, count in sorted(
        log_grouper.message_counts.items(), 
        key=lambda x: x[1], 
        reverse=True
    )[:10]:
        # Extract component and message from the key
        parts = key.split(":", 2)
        if len(parts) >= 3:
            component, level, message = parts
            top_grouped.append({
                "component": component,
                "level": level,
                "message": message,
                "count": count
            })
    
    return {
        "total_groups": total_groups,
        "total_grouped_messages": total_grouped_messages,
        "window_seconds": log_grouper.window_seconds,
        "top_grouped": top_grouped
    }


def reset_log_grouping_stats() -> None:
    """
    Reset the log grouping statistics.
    """
    global log_grouper
    log_grouper.message_counts.clear()
    log_grouper.last_logged.clear()
    logger.info("Log grouping statistics reset")
