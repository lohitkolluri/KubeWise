import os
import sys
import threading
import json
import traceback
from typing import Dict, Any, Optional, List, Union
from contextvars import ContextVar
from functools import wraps

from loguru import logger

from app.core.config import settings

# Global flag to control console logging
_console_logging_enabled = True
_console_logging_lock = threading.Lock()

# Context variables for request tracking
request_id_var: ContextVar[str] = ContextVar('request_id', default='')
operation_name_var: ContextVar[str] = ContextVar('operation', default='')
namespace_var: ContextVar[str] = ContextVar('namespace', default='')
component_var: ContextVar[str] = ContextVar('component', default='main')

# Structured logging context
class LogContext:
    """Class to manage the logging context"""
    def __init__(
        self, 
        request_id: Optional[str] = None, 
        operation: Optional[str] = None,
        namespace: Optional[str] = None,
        component: Optional[str] = None,
        resource_type: Optional[str] = None
    ):
        self.request_id = request_id
        self.operation = operation
        self.namespace = namespace
        self.component = component
        self.resource_type = resource_type
        self._tokens = {}

    def __enter__(self):
        if self.request_id:
            self._tokens['request_id'] = request_id_var.set(self.request_id)
        if self.operation:
            self._tokens['operation'] = operation_name_var.set(self.operation)
        if self.namespace:
            self._tokens['namespace'] = namespace_var.set(self.namespace)
        if self.component:
            self._tokens['component'] = component_var.set(self.component)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for token in self._tokens.values():
            pass  # Context vars reset automatically when the function returns

def log_context(
    request_id: Optional[str] = None, 
    operation: Optional[str] = None,
    namespace: Optional[str] = None,
    component: Optional[str] = None
):
    """Decorator to add context to logging within a function"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with LogContext(request_id, operation, namespace, component):
                return func(*args, **kwargs)
        return wrapper
    return decorator

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

def inject_context_to_record(record):
    """Inject context variables into log records."""
    # Add request_id if available
    request_id = request_id_var.get()
    if request_id:
        record["extra"]["request_id"] = request_id
    
    # Add operation name if available
    operation = operation_name_var.get()
    if operation:
        record["extra"]["operation"] = operation
    
    # Add namespace if available
    namespace = namespace_var.get()
    if namespace:
        record["extra"]["namespace"] = namespace
    
    # Add component if available
    component = component_var.get()
    record["extra"]["component"] = component
    
    return record

def format_exception(exception: Exception) -> Dict[str, Any]:
    """Format an exception into a dictionary for structured logging."""
    exception_dict = {
        "type": exception.__class__.__name__,
        "message": str(exception),
        "traceback": traceback.format_exc().split('\n')
    }
    
    # Add extra details for our custom exceptions
    if hasattr(exception, 'details') and exception.details:
        exception_dict["details"] = exception.details
        
    return exception_dict

def log_exception(exception: Exception, level: str = "ERROR", include_traceback: bool = True, **kwargs) -> None:
    """
    Log an exception with structured details.
    
    Args:
        exception: The exception to log
        level: Log level (ERROR, CRITICAL, etc.)
        include_traceback: Whether to include traceback in the log
        **kwargs: Additional context to include in the log
    """
    exception_dict = format_exception(exception)
    
    if not include_traceback:
        exception_dict.pop("traceback", None)
    
    log_message = f"Exception: {exception_dict['type']}: {exception_dict['message']}"
    
    # Use the appropriate log level
    if level == "CRITICAL":
        logger.critical(log_message, exception=exception_dict, **kwargs)
    elif level == "WARNING":
        logger.warning(log_message, exception=exception_dict, **kwargs)
    else:
        logger.error(log_message, exception=exception_dict, **kwargs)

def setup_logger() -> None:
    """
    Configures Loguru logger with enhanced visual formatting and structured logging.
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

    # Enhanced format with emoji indicators for different log levels and structured context
    enhanced_log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "{extra[emoji]} "
        "{extra[context]} "
        "<level>{message}</level>"
    )

    # Add JSON formatter for structured machine-readable logs
    def json_formatter(record):
        """Format log record as JSON for machine parsing."""
        # Standard log fields
        log_entry = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "message": record["message"],
            "module": record["name"],
            "function": record["function"],
            "line": record["line"],
        }
        
        # Add context fields if present
        for context_field in ["request_id", "operation", "namespace", "component"]:
            if context_field in record["extra"]:
                log_entry[context_field] = record["extra"][context_field]
        
        # Add exception details if present
        if "exception" in record["extra"]:
            log_entry["exception"] = record["extra"]["exception"]
        
        # Add any other extra fields
        for key, value in record["extra"].items():
            if key not in ["emoji", "request_id", "operation", "namespace", "component", "exception"]:
                log_entry[key] = value
        
        return json.dumps(log_entry)

    # Configure default extras
    logger.configure(
        extra={
            "emoji": "ℹ️ ",
            "request_id": "",
            "operation": "",
            "namespace": "",
            "component": "main",
            "context": ""
        }
    )

    # Function to dynamically set emoji based on log level and inject context
    def record_processor(record):
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
        
        # Inject context variables
        inject_context_to_record(record)
        
        # Build a simplified context string - only include critical identifiers
        context_parts = []
        
        # Only include the first 8 chars of request ID if it exists
        if record["extra"].get("request_id"):
            req_id = record["extra"]["request_id"]
            short_req_id = req_id[:8] if len(req_id) > 8 else req_id
            context_parts.append(f"[{short_req_id}]")
            
        # Only include essential context tags
        if record["extra"].get("operation") and record["extra"]["operation"] not in ["startup", "application"]:
            context_parts.append(f"[{record['extra']['operation']}]")
            
        record["extra"]["context"] = " ".join(context_parts)
        
        return record

    # Console handler with enhanced format - with filter to control when output appears
    logger.add(
        sys.stderr,
        format=enhanced_log_format,
        level=settings.LOG_LEVEL,
        enqueue=True,
        filter=lambda record: record_processor(record) and console_logging_filter(record),
        colorize=True,
    )

    # File handler with standard format
    os.makedirs("logs", exist_ok=True)  # Ensure logs directory exists
    logger.add(
        "logs/app.log",
        rotation="500 MB",
        retention="10 days",
        format=log_format,
        level=settings.LOG_LEVEL,
        enqueue=True,
        filter=record_processor,
    )
    
    # JSON structured logs for machine processing
    logger.add(
        "logs/app.json.log",
        rotation="500 MB",
        retention="10 days",
        serialize=True,  # Use serializer instead of format
        level=settings.LOG_LEVEL,
        enqueue=True,
        filter=record_processor,
    )

    logger.success("Logger configured successfully with enhanced formatting and structured logging")

# Convenience functions for logging with context
def debug(message: str, **kwargs) -> None:
    """Log a debug message with optional context."""
    logger.debug(message, **kwargs)

def info(message: str, **kwargs) -> None:
    """Log an info message with optional context."""
    logger.info(message, **kwargs)

def success(message: str, **kwargs) -> None:
    """Log a success message with optional context."""
    logger.success(message, **kwargs)

def warning(message: str, **kwargs) -> None:
    """Log a warning message with optional context."""
    logger.warning(message, **kwargs)

def error(message: str, exception: Optional[Exception] = None, **kwargs) -> None:
    """Log an error message with optional exception details and context."""
    if exception:
        log_exception(exception, level="ERROR", **kwargs)
    else:
        logger.error(message, **kwargs)

def critical(message: str, exception: Optional[Exception] = None, **kwargs) -> None:
    """Log a critical message with optional exception details and context."""
    if exception:
        log_exception(exception, level="CRITICAL", **kwargs)
    else:
        logger.critical(message, **kwargs)
