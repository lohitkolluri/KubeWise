"""
CLI aesthetics module for KubeWise.
Provides ASCII banners, spinners, progress bars, and formatted status dashboards.
"""

import functools
import sys
import threading
import time
from enum import Enum
from typing import Dict, Optional

from app.core.logger import disable_console_logging, enable_console_logging

# --- Constants ---
CYAN = "\033[1;36m"
WHITE = "\033[1;97m"
RESET = "\033[0m"

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

SHUTDOWN_BANNER = r"""
╔═══════════════════════════════════════════════════════════════════╗
║                     SHUTTING DOWN KUBEWISE                        ║
╚═══════════════════════════════════════════════════════════════════╝
"""

# --- Decorator ---
def pause_logging(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        disable_console_logging()
        try:
            return func(*args, **kwargs)
        finally:
            enable_console_logging()
    return wrapper

# --- Enums ---
class ServiceStatus(Enum):
    UNKNOWN = "❓"
    HEALTHY = "✅"
    WARNING = "⚠️"
    ERROR = "❌"
    INITIALIZING = "🔄"
    STOPPING = "🛑"

# --- Spinner ---
class SpinnerThread(threading.Thread):
    def __init__(self, message="Loading...", delay=0.1):
        super().__init__(daemon=True)
        self.message = message
        self.delay = delay
        self.running = True
        self.spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.counter = 0
        disable_console_logging()

    def run(self):
        while self.running:
            char = self.spinner_chars[self.counter % len(self.spinner_chars)]
            sys.stdout.write(f"\r\033[K{CYAN}{char}{RESET} {self.message}")
            sys.stdout.flush()
            time.sleep(self.delay)
            self.counter += 1

    def stop(self, success=True, message=None):
        self.running = False
        icon = "✅" if success else "❌"
        final = message or self.message
        sys.stdout.write(f"\r\033[K{icon} {final}\n")
        sys.stdout.flush()
        enable_console_logging()

# --- CLI Utilities ---
@pause_logging
def print_ascii_banner(banner_text=STARTUP_BANNER, color=CYAN):
    print(f"{color}{banner_text}{RESET}")

@pause_logging
def print_version_info(version="3.1.0", mode="MANUAL"):
    print(
        f"""{WHITE}
╔════════════════════════════════════════════════════════════╗
║ \033[1;93mKubeWise\033[1;97m v{version}                                            ║
║ Mode: \033[1;92m{mode.upper()}                                                 \033[1;97m║
║ AI-Powered Kubernetes Anomaly Detection & Remediation      ║
╚════════════════════════════════════════════════════════════╝
{RESET}"""
    )

def start_spinner(message="Loading..."):
    spinner = SpinnerThread(message)
    spinner.start()
    return spinner

@pause_logging
def format_service_status(service_name: str, status: ServiceStatus, message: Optional[str] = None):
    color_map = {
        ServiceStatus.HEALTHY: "\033[1;92m",
        ServiceStatus.WARNING: "\033[1;93m",
        ServiceStatus.ERROR: "\033[1;91m",
        ServiceStatus.UNKNOWN: "\033[1;90m",
        ServiceStatus.INITIALIZING: "\033[1;94m",
        ServiceStatus.STOPPING: "\033[1;95m",
    }
    color = color_map.get(status, RESET)
    msg = f" - {message}" if message else ""
    return f"{color}{status.value}{RESET} {service_name}{msg}"

@pause_logging
def print_service_dashboard(services_status: Dict[str, Dict]):
    print(f"\n{WHITE}╔══════════════════════ SERVICE STATUS ═══════════════════════╗{RESET}")
    for name, info in services_status.items():
        status_str = info.get("status", "unknown").lower()
        status = {
            "healthy": ServiceStatus.HEALTHY,
            "warning": ServiceStatus.WARNING,
            "unhealthy": ServiceStatus.ERROR,
            "error": ServiceStatus.ERROR,
            "initializing": ServiceStatus.INITIALIZING,
        }.get(status_str, ServiceStatus.UNKNOWN)
        print(f"{WHITE}║{RESET} {format_service_status(name, status, info.get('message'))}")
    print(f"{WHITE}╚═════════════════════════════════════════════════════════════╝{RESET}")

@pause_logging
def print_progress_bar(iteration, total, prefix="", suffix="", length=50, fill="█", print_end="\r"):
    percent = "{0:.1f}".format(100 * (iteration / float(total)))
    filled = int(length * iteration // total)
    bar = fill * filled + "-" * (length - filled)
    print(f"\r{prefix} |{bar}| {percent}% {suffix}", end=print_end)
    if iteration == total:
        print()

@pause_logging
def print_shutdown_message():
    print(f"\n\033[1;95m{SHUTDOWN_BANNER}{RESET}")
    print(
        f"""
{WHITE}╔══════════════════════════════════════════════════════════╗
║ Thank you for using KubeWise! Goodbye.                   ║
╚══════════════════════════════════════════════════════════╝
{RESET}"""
    )

@pause_logging
def print_kubewise_logo():
    print_ascii_banner()
