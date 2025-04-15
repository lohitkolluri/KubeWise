"""
CLI aesthetics module for KubeWise.
Provides ASCII art, spinners, progress bars and other visual elements for CLI output.
"""
from typing import Dict, List, Optional
import time
import threading
import sys
from enum import Enum
import functools
from app.core.logger import disable_console_logging, enable_console_logging

# ASCII art for startup banner
STARTUP_BANNER = r"""
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║   ██╗  ██╗██╗   ██╗██████╗ ███████╗██╗    ██╗██╗███████╗███████╗  ║
║   ██║ ██╔╝██║   ██║██╔══██╗██╔════╝██║    ██║██║██╔════╝██╔════╝  ║
║   █████╔╝ ██║   ██║██████╔╝█████╗  ██║ █╗ ██║██║███████╗█████╗    ║
║   ██╔═██╗ ██║   ██║██╔══██╗██╔══╝  ██║███╗██║██║╚════██║██╔══╝    ║
║   ██║  ██╗╚██████╔╝██████╔╝███████╗╚███╔███╔╝██║███████║███████╗  ║
║   ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝ ╚══╝╚══╝ ╚═╝╚══════╝╚══════╝  ║
║                                                                   ║
╚═══════════════════════════════════════════════════════════════════╝
"""

# ASCII art for shutdown banner
SHUTDOWN_BANNER = r"""
╔═══════════════════════════════════════════════════════════════════╗
║                     SHUTTING DOWN KUBEWISE                        ║
╚═══════════════════════════════════════════════════════════════════╝
"""

def pause_logging(func):
    """
    Decorator to temporarily disable console logging during CLI visual elements.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        disable_console_logging()
        try:
            result = func(*args, **kwargs)
        finally:
            # Ensure logging is re-enabled even if an exception occurs
            enable_console_logging()
        return result
    return wrapper

class ServiceStatus(Enum):
    """Service status indicators for the dashboard."""
    UNKNOWN = "❓"
    HEALTHY = "✅"
    WARNING = "⚠️"
    ERROR = "❌"
    INITIALIZING = "🔄"
    STOPPING = "🛑"

class SpinnerThread(threading.Thread):
    """Thread for displaying a spinner animation in the console."""
    def __init__(self, message="Loading...", delay=0.1):
        super().__init__()
        self.message = message
        self.delay = delay
        self.running = True
        self.spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.counter = 0
        self.daemon = True  # Thread will exit when main program exits
        # Disable logging when spinner starts
        disable_console_logging()

    def run(self):
        """Run the spinner animation."""
        while self.running:
            char = self.spinner_chars[self.counter % len(self.spinner_chars)]
            sys.stdout.write(f"\r\033[K\033[1;36m{char}\033[0m {self.message}")
            sys.stdout.flush()
            time.sleep(self.delay)
            self.counter += 1

    def stop(self, success=True, message=None):
        """Stop the spinner animation."""
        self.running = False
        result_char = "✅" if success else "❌"
        final_message = message if message else self.message
        sys.stdout.write(f"\r\033[K{result_char} {final_message}\n")
        sys.stdout.flush()
        # Re-enable logging when spinner stops
        enable_console_logging()


@pause_logging
def print_ascii_banner(banner_text=STARTUP_BANNER, color="\033[1;36m"):
    """Print an ASCII art banner with color."""
    print(f"{color}{banner_text}\033[0m")


@pause_logging
def print_version_info(version="3.1.0", mode="MANUAL"):
    """Print version information with colorful formatting."""
    print("\033[1;97m╔════════════════════════════════════════════════════════════╗\033[0m")
    print(f"\033[1;97m║ \033[1;93mKubeWise \033[1;97mv{version}                                            \033[1;97m║\033[0m")
    print(f"\033[1;97m║ \033[0;37mMode: \033[1;92m{mode.upper()}                                                 \033[1;97m║\033[0m")
    print(f"\033[1;97m║ \033[0;37mAI-Powered Kubernetes Anomaly Detection & Remediation      \033[1;97m║\033[0m")
    print("\033[1;97m╚════════════════════════════════════════════════════════════╝\033[0m")


def start_spinner(message="Loading..."):
    """Start a spinner animation with the given message."""
    spinner = SpinnerThread(message)
    spinner.start()
    return spinner


@pause_logging
def format_service_status(service_name: str, status: ServiceStatus, message: Optional[str] = None):
    """Format a service status line for the dashboard."""
    status_color = {
        ServiceStatus.HEALTHY: "\033[1;92m",      # Bright Green
        ServiceStatus.WARNING: "\033[1;93m",      # Bright Yellow
        ServiceStatus.ERROR: "\033[1;91m",        # Bright Red
        ServiceStatus.UNKNOWN: "\033[1;90m",      # Bright Black (Gray)
        ServiceStatus.INITIALIZING: "\033[1;94m", # Bright Blue
        ServiceStatus.STOPPING: "\033[1;95m",     # Bright Magenta
    }

    color = status_color.get(status, "\033[0m")
    message_str = f" - {message}" if message else ""

    return f"{color}{status.value}\033[0m {service_name}{message_str}"


@pause_logging
def print_service_dashboard(services_status: Dict[str, Dict]):
    """Print a dashboard of service statuses."""
    print("\n\033[1;97m╔══════════════════════ SERVICE STATUS ═══════════════════════╗\033[0m")

    for service_name, info in services_status.items():
        status = ServiceStatus.UNKNOWN
        if info.get("status") == "healthy":
            status = ServiceStatus.HEALTHY
        elif info.get("status") == "warning":
            status = ServiceStatus.WARNING
        elif info.get("status") == "unhealthy" or info.get("status") == "error":
            status = ServiceStatus.ERROR
        elif info.get("status") == "initializing":
            status = ServiceStatus.INITIALIZING

        print(f"\033[1;97m║\033[0m {format_service_status(service_name, status, info.get('message'))}")

    print("\033[1;97m╚═════════════════════════════════════════════════════════════╝\033[0m")


@pause_logging
def print_progress_bar(iteration, total, prefix='', suffix='', length=50, fill='█', print_end="\r"):
    """Print a progress bar in the terminal."""
    percent = ("{0:.1f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + '-' * (length - filled_length)
    print(f'\r{prefix} |{bar}| {percent}% {suffix}', end=print_end)
    if iteration == total:
        print()


@pause_logging
def print_shutdown_message():
    """Print a shutdown message."""
    print(f"\n\033[1;95m{SHUTDOWN_BANNER}\033[0m")
    print("\n\033[1;97m╔══════════════════════════════════════════════════════════╗\033[0m")
    print("\033[1;97m║\033[0m Thank you for using KubeWise! Goodbye.                   \033[1;97m║\033[0m")
    print("\033[1;97m╚══════════════════════════════════════════════════════════╝\033[0m")


@pause_logging
def print_kubewise_logo():
    """Print the KubeWise logo in bright cyan."""
    print(f"\033[1;96m{STARTUP_BANNER}\033[0m")
