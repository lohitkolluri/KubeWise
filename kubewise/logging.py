import sys
import logging
import os
from pathlib import Path

from loguru import logger
from rich.logging import RichHandler

from kubewise.config import settings

# Standardized log format for consistent appearance across outputs
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

# Log rotation settings to prevent logs from growing too large
LOG_ROTATION = "10 MB"
LOG_RETENTION = "7 days"

# Log directory - configurable via env var or defaulting to ./logs
LOG_DIR = Path(os.getenv("KUBEWISE_LOG_DIR", "./logs"))
LOG_FILE = LOG_DIR / "kubewise.log"


class InterceptHandler(logging.Handler):
    """
    Intercept standard logging messages and redirect them to Loguru.
    
    This handler bridges Python's standard library logging to Loguru,
    ensuring consistent formatting for all log messages regardless of source.
    """
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the original caller location to maintain proper source tracking
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back # type: ignore
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging() -> None:
    """
    Configure Loguru logger based on application settings.
    
    Establishes console logging with rich formatting and optional file logging
    with rotation. Also intercepts standard library logging for unified output.
    """
    # Remove default handler
    logger.remove()

    # Add console handler using RichHandler for better terminal output
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

    # Add file handler if directory exists or can be created
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(
            LOG_FILE,
            level=settings.log_level.upper(),
            format=LOG_FORMAT,
            rotation=LOG_ROTATION,
            retention=LOG_RETENTION,
            enqueue=True,    # Async logging for better performance
            backtrace=True,
            diagnose=True,   # Include extra diagnostic info for exceptions
            colorize=False,  # Avoid ANSI color codes in log files
        )
        logger.info(f"File logging enabled: {LOG_FILE}")
    except PermissionError:
        logger.warning(
            f"Permission denied creating log directory {LOG_DIR}. "
            "File logging disabled. Ensure the directory exists and is writable."
        )
    except Exception as e:
        logger.error(f"Failed to set up file logging to {LOG_FILE}: {e}")

    # Intercept standard logging to unify log appearance
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logger.info(f"Loguru setup complete. Level: {settings.log_level}. Intercepting standard logging.")

# This module can be executed directly to test logging configuration
if __name__ == "__main__":
    print("Setting up logging for standalone test...")
    setup_logging()
    logger.info("Logging setup tested.")
    logging.getLogger("uvicorn.error").warning("Testing standard logging interception.")
