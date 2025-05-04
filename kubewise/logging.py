import sys
import logging
import os
from pathlib import Path

from loguru import logger
from rich.logging import RichHandler # Import RichHandler

from kubewise.config import settings

# Define a standard log format
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

# Define log file rotation settings (adjust as needed)
LOG_ROTATION = "10 MB"  # Rotate when file reaches 10 MB
LOG_RETENTION = "7 days" # Keep logs for 7 days

# Define the log directory, making it configurable via env var, fallback to local ./logs
LOG_DIR = Path(os.getenv("KUBEWISE_LOG_DIR", "./logs"))
LOG_FILE = LOG_DIR / "kubewise.log"


class InterceptHandler(logging.Handler):
    """
    Intercept standard logging messages and redirect them to Loguru.
    See: https://loguru.readthedocs.io/en/stable/overview.html#entirely-compatible-with-standard-logging
    """
    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
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

    Sets up console logging and optional file logging with rotation.
    Intercepts standard library logging.
    """
    # Remove default handler
    logger.remove()

    # Add console handler using RichHandler for better formatting
    logger.add(
        RichHandler(
            level=settings.log_level.upper(),
            show_path=False, # Keep logs cleaner, path is in format
            markup=True,     # Enable rich console markup
            rich_tracebacks=True, # Use rich for tracebacks
            log_time_format="[%Y-%m-%d %H:%M:%S.%f]", # Match format style
        ),
        format=LOG_FORMAT, # Keep custom format for now
        level=settings.log_level.upper(), # Set level on handler too
    )

    # Add file handler if log directory exists or can be created
    # In K8s, the directory should be mounted via PVC
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(
            LOG_FILE,
            level=settings.log_level.upper(),
            format=LOG_FORMAT,
            rotation=LOG_ROTATION,
            retention=LOG_RETENTION,
            enqueue=True,  # Make file logging asynchronous
            backtrace=True, # Show full stack traces
            diagnose=True,  # Add extra details to exceptions
            colorize=False, # No colors in file logs
        )
        logger.info(f"File logging enabled: {LOG_FILE}")
    except PermissionError:
        logger.warning(
            f"Permission denied creating log directory {LOG_DIR}. "
            "File logging disabled. Ensure the directory exists and is writable."
        )
    except Exception as e:
        logger.error(f"Failed to set up file logging to {LOG_FILE}: {e}")


    # Intercept standard logging messages (e.g., from libraries like uvicorn, httpx)
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logger.info(f"Loguru setup complete. Level: {settings.log_level}. Intercepting standard logging.")

    # Example log messages
    # logger.debug("This is a debug message.")
    # logger.info("This is an info message.")
    # logger.warning("This is a warning message.")
    # logger.error("This is an error message.")
    # logger.critical("This is a critical message.")

# Automatically configure logging when this module is imported
setup_logging() # Consider calling this explicitly during app startup instead

if __name__ == "__main__":
    # Example of how to use the setup function
    print("Setting up logging for standalone test...")
    setup_logging()
    logger.info("Logging setup tested.")
    logging.getLogger("uvicorn.error").warning("Testing standard logging interception.")
