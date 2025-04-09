import json
import logging
import sys
from typing import Dict, Any

from loguru import logger


class InterceptHandler(logging.Handler):
    """
    Intercept standard logging and redirect to loguru.
    """
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


class JsonSink:
    """
    Custom JSON formatter for loguru.
    """
    def __call__(self, record):
        record_dict = record["record"]
        log_dict = {
            "timestamp": record_dict["time"].strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record_dict["level"].name,
            "message": record_dict["message"],
            "module": record_dict["name"],
        }

        if record_dict["exception"]:
            log_dict["exception"] = str(record_dict["exception"])

        # Add extra attributes if present
        for k, v in record_dict["extra"].items():
            log_dict[k] = v

        return json.dumps(log_dict)


def setup_logging(json_logs: bool = False, log_level: str = "INFO"):
    """
    Configure logging with loguru.

    Args:
        json_logs: If True, output logs in JSON format
        log_level: Log level to use (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    # Intercept standard logging
    logging.basicConfig(handlers=[InterceptHandler()], level=0)

    # Remove default loguru handler
    logger.remove()

    # Configure loguru
    if json_logs:
        logger.configure(
            handlers=[
                {
                    "sink": sys.stdout,
                    "serialize": True,
                    "format": JsonSink(),
                    "level": log_level,
                }
            ]
        )
    else:
        logger.configure(
            handlers=[
                {
                    "sink": sys.stdout,
                    "format": "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
                    "level": log_level,
                }
            ]
        )

    # Intercept uvicorn logs
    for _log in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"]:
        _logger = logging.getLogger(_log)
        _logger.handlers = [InterceptHandler()]

    return logger
