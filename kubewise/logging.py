# -*- coding: utf-8 -*-
import logging
import os
from pathlib import Path

from loguru import logger
from rich.logging import RichHandler

from kubewise.config import settings

LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)
LOG_ROTATION = "10 MB"
LOG_RETENTION = "7 days"
LOG_DIR = Path(os.getenv("KUBEWISE_LOG_DIR", "./logs"))
LOG_FILE = LOG_DIR / "kubewise.log"


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

    Sets up rich console output, file logging with rotation, and intercepts
    standard library logs for unified output.
    """
    logger.remove()

    # Configure console logging via RichHandler.
    logger.add(
        RichHandler(
            level=settings.log_level.upper(),
            show_path=False,
            markup=True,
            rich_tracebacks=True,
            log_time_format="[%Y-%m-%d %H:%M:%S.%f]",
        ),
        format=LOG_FORMAT,
        level=settings.log_level.upper(),
    )

    # Configure file logging with rotation.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(
            LOG_FILE,
            level=settings.log_level.upper(),
            format=LOG_FORMAT,
            rotation=LOG_ROTATION,
            retention=LOG_RETENTION,
            enqueue=True,  # Use asynchronous logging for performance.
            backtrace=True,
            diagnose=True,  # Provides extended exception details.
            colorize=False,  # No ANSI colors in files.
        )
        logger.info(f"File logging enabled: {LOG_FILE}")
    except PermissionError:
        logger.warning(
            f"Permission denied creating log directory {LOG_DIR}. File logging disabled."
        )
    except Exception as e:
        logger.error(f"Failed to set up file logging to {LOG_FILE}: {e}")

    # Intercept standard Python logging.
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logger.info(
        f"Loguru setup complete. Level: {settings.log_level}. Intercepting standard logging."
    )
