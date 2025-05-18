#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KubeWise Interactive CLI

A professional command-line interface for the KubeWise application.
This CLI provides real-time monitoring and remediation capabilities for
Kubernetes clusters, with a focus on CrashLoopBackOff and ImagePullBackOff failures.

To install dependencies:
pip install typer==0.9.0 rich httpx python-dateutil

To use:
chmod +x kubewise_cli.py
./kubewise_cli.py --help
"""

import asyncio
import json
import os
import sys
import time
import logging
import configparser
import signal
from typing import Dict, List, Optional, Tuple, Any, Union, Set
from pathlib import Path
from datetime import datetime, timezone

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich import box
from rich.syntax import Syntax
from rich.live import Live
from rich.prompt import Prompt, Confirm
from rich.logging import RichHandler
from rich.theme import Theme
from rich.markdown import Markdown
from rich.style import Style
from rich.text import Text

# Global variables to track running tasks for clean shutdown
RUNNING_TASKS: Set[asyncio.Task] = set()
SHUTDOWN_EVENT = asyncio.Event()

# Define rich theme for consistent styling
RICH_THEME = Theme({
    "info": "dim cyan",
    "warning": "yellow",
    "danger": "bold red",
    "success": "bold green",
    "command": "bold blue",
    "heading": "bold white on blue",
    "dim": "dim",
    "title": "bold cyan",
    "pod.running": "green",
    "pod.pending": "yellow",
    "pod.failed": "red",
    "pod.unknown": "dim",
    "anomaly.critical": "bold red",
    "anomaly.high": "red",
    "anomaly.medium": "yellow",
    "anomaly.low": "cyan",
})

# Initialize Typer app and Rich console with theme
app = typer.Typer(
    help="KubeWise: AI-Powered Kubernetes Guardian - Interactive CLI",
    add_completion=False,  # Disable Typer's built-in completion to use our custom one
    no_args_is_help=True,  # Show help when no command is provided
    pretty_exceptions_show_locals=False  # Disable rich exception formatting
)
console = Console(theme=RICH_THEME, highlight=True)

# Import Kubewise logging
from kubewise.logging import setup_logging, get_logger

# Set up logging with our custom configuration
setup_logging()
log = get_logger("cli")

# ASCII art banner for KubeWise (improved)
ASCII_BANNER = """
██╗  ██╗██╗   ██╗██████╗ ███████╗██╗    ██╗██╗███████╗███████╗
██║ ██╔╝██║   ██║██╔══██╗██╔════╝██║    ██║██║██╔════╝██╔════╝
█████╔╝ ██║   ██║██████╔╝█████╗  ██║ █╗ ██║██║███████╗█████╗  
██╔═██╗ ██║   ██║██╔══██╗██╔══╝  ██║███╗██║██║╚════██║██╔══╝  
██║  ██╗╚██████╔╝██████╔╝███████╗╚███╔███╔╝██║███████║███████╗
╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝ ╚══╝╚══╝ ╚═╝╚══════╝╚══════╝
"""

# Constants
DEFAULT_CONFIG_PATH = Path.home() / ".kubewise" / "config.ini"
DEFAULT_API_URL = "http://localhost:8000"

# CLI configuration
class Config:
    """CLI Configuration class"""
    def __init__(self):
        self.api_url = DEFAULT_API_URL
        self.namespace = "default"
        self.log_level = "INFO"
        self.kube_context = None
        self.load_config()
    
    def load_config(self):
        """Load configuration from file if exists"""
        config_path = Path(os.environ.get("KUBEWISE_CONFIG", DEFAULT_CONFIG_PATH))
        
        if not config_path.exists():
            # Create default config if it doesn't exist
            config_path.parent.mkdir(parents=True, exist_ok=True)
            self._create_default_config(config_path)
            return
            
        try:
            config = configparser.ConfigParser()
            config.read(config_path)
            
            if "api" in config:
                self.api_url = config["api"].get("url", DEFAULT_API_URL)
            
            if "cli" in config:
                self.log_level = config["cli"].get("log_level", "INFO")
                self.namespace = config["cli"].get("namespace", "default")
            
            if "kubernetes" in config:
                self.kube_context = config["kubernetes"].get("context", None)
                
            # Set log level
            logging.getLogger("kubewise-cli").setLevel(getattr(logging, self.log_level))
            
        except Exception as e:
            log.warning(f"Error loading config: {e}")
    
    def _create_default_config(self, config_path: Path):
        """Create default configuration file"""
        config = configparser.ConfigParser()
        config["api"] = {
            "url": DEFAULT_API_URL,
        }
        config["cli"] = {
            "log_level": "INFO",
            "namespace": "default",
        }
        config["kubernetes"] = {
            "context": "",
        }
        
        with open(config_path, "w") as f:
            config.write(f)
        
        log.info(f"Created default config at {config_path}")

# Global config instance
cli_config = Config()

# API endpoint configuration from config
API_BASE_URL = cli_config.api_url

# Global flag for mock mode
MOCK_MODE = False

# Status emojis for visual indication
STATUS_EMOJIS = {
    "running": "🟢",
    "pending": "🟡",
    "failed": "🔴",
    "unknown": "❓",
    "success": "✅",
    "error": "❌",
    "warning": "⚠️",
    "info": "ℹ️",
}


def press_to_continue():
    """Helper function to display a consistent 'press to continue' prompt."""
    console.print("\nPress Enter to continue...")
    input()


def display_banner():
    """Display the KubeWise ASCII banner with professional styling."""
    console.clear()
    
    # Create a multi-part banner
    panel = Panel.fit(
        ASCII_BANNER,
        title="[bold green]KubeWise",
        subtitle="AI-Powered Kubernetes Guardian 🚀",
        style="bold cyan",
        box=box.DOUBLE,
    )
    
    # Display the banner
    console.print(panel)
    
    # Show connection info and version
    info_text = Text()
    info_text.append("Connected to: ", style="dim")
    info_text.append(f"{API_BASE_URL}", style="bold blue")
    
    if cli_config.kube_context:
        info_text.append(" | Kubernetes Context: ", style="dim")
        info_text.append(f"{cli_config.kube_context}", style="bold green")
    
    info_text.append(" | Default Namespace: ", style="dim")
    info_text.append(f"{cli_config.namespace}", style="bold yellow")
    
    console.print(info_text)
    console.print("", Style(dim=True))


async def api_request(
    endpoint: str, 
    method: str = "GET", 
    data: Optional[Dict] = None,
    params: Optional[Dict] = None,
    timeout: float = 10.0,
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> Union[Dict, List]:
    """
    Make an API request to the KubeWise API server with retries.
    
    Args:
        endpoint: API endpoint path
        method: HTTP method (GET, POST, etc.)
        data: Optional JSON data for POST/PUT requests
        params: Optional query parameters
        timeout: Request timeout in seconds
        max_retries: Maximum number of retry attempts
        retry_delay: Delay between retries in seconds
        
    Returns:
        API response as dictionary or list
    """
    # For testing purposes - mock response if requested
    if MOCK_MODE:
        return mock_api_response(endpoint)
    
    url = f"{API_BASE_URL}{endpoint}"
    
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, max_retries + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    json=data,
                    params=params,
                )
                
                if response.status_code == 404:
                    error_text = f"Resource not found: {endpoint}"
                    console.print(f"[danger]{error_text}[/danger]")
                    return {"error": error_text, "status_code": 404}
                
                response.raise_for_status()
                return response.json()
                
            except httpx.HTTPStatusError as e:
                error_text = f"HTTP Error: {e.response.status_code} - {e.response.reason_phrase}"
                
                if attempt == max_retries:
                    console.print(f"[danger]{error_text}[/danger]")
                    return {"error": error_text, "status_code": e.response.status_code}
                    
            except httpx.ConnectError:
                error_text = f"Connection Error: Could not connect to API server at {API_BASE_URL}"
                
                if attempt == max_retries:
                    console.print(f"[danger]{error_text}[/danger]")
                    console.print(f"[info]Is the KubeWise API server running at {API_BASE_URL}?[/info]")
                    console.print("[info]You can use --mock flag to use mock data for testing[/info]")
                    return {"error": error_text}
                    
            except httpx.ReadTimeout:
                error_text = f"Timeout Error: The API server at {API_BASE_URL} did not respond in time"
                
                if attempt == max_retries:
                    console.print(f"[danger]{error_text}[/danger]")
                    return {"error": error_text}
                
            # Wait before retrying
            if attempt < max_retries:
                await asyncio.sleep(retry_delay * attempt)  # Exponential backoff
    
    # Should never reach here, but just in case
    return {"error": "Maximum retries exceeded"}


def mock_api_response(endpoint: str) -> Union[Dict, List]:
    """Return mock responses for testing when the API server is not available."""
    # Use a fixed timestamp for consistent mock IDs during the same session
    mock_timestamp = int(time.time() // 100) * 100
    mock_anomaly_id_1 = f"mock-anomaly-{mock_timestamp}"
    mock_anomaly_id_2 = f"mock-anomaly-{mock_timestamp+1}"
    mock_plan_id_1 = f"mock-plan-{mock_timestamp}"
    mock_plan_id_2 = f"mock-plan-{mock_timestamp+1}"
    
    # Return empty or minimal valid responses for all endpoints
    if endpoint == "/anomalies":
        # Return a list of mock anomalies
        return [
            {
                "id": mock_anomaly_id_1,
                "entity_id": "mock-pod-1",
                "entity_type": "Pod",
                "failure_reason": "High CPU utilization",
                "status": "Active",
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "remediation_status": "None"
            },
            {
                "id": mock_anomaly_id_2,
                "entity_id": "mock-pod-2",
                "entity_type": "Pod",
                "failure_reason": "Memory leak",
                "status": "Active",
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "remediation_status": "None"
            }
        ]
    elif endpoint == "/remediation/plans":
        # Return a list of mock remediation plans for display
        return [
            {
                "id": mock_plan_id_1,
                "_id": mock_plan_id_1,
                "anomaly_id": mock_anomaly_id_1,
                "target_entity_id": "mock-pod-1",
                "target_entity_type": "Pod",
                "plan_name": "CPU Remediation Plan",
                "description": "Plan to address high CPU utilization",
                "status": "pending",
                "actions": [
                    {
                        "action_type": "restart_pod",
                        "parameters": {"namespace": "default", "name": "mock-pod-1"},
                        "execution_status": "pending"
                    }
                ],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            },
            {
                "id": mock_plan_id_2,
                "_id": mock_plan_id_2,
                "anomaly_id": mock_anomaly_id_2,
                "target_entity_id": "mock-pod-2",
                "target_entity_type": "Pod",
                "plan_name": "Memory Remediation Plan",
                "description": "Plan to address memory leak",
                "status": "pending",
                "actions": [
                    {
                        "action_type": "update_resource_limits",
                        "parameters": {"namespace": "default", "name": "mock-pod-2", "memory_limit": "512Mi"},
                        "execution_status": "pending"
                    }
                ],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
        ]
    elif endpoint.startswith("/remediation/plans/"):
        # Specific plan endpoint - return a properly formatted mock plan
        plan_id = endpoint.split("/")[-1]
        return {
            "id": plan_id,
            "_id": plan_id,
            "anomaly_id": mock_anomaly_id_1,
            "plan_name": "Mock Remediation Plan",
            "description": "This is a mock remediation plan for testing",
            "target_entity_id": "example-pod-123",
            "target_entity_type": "Pod",
            "actions": [
                {
                    "action_type": "restart_pod",
                    "parameters": {"namespace": "default", "name": "example-pod-123"},
                    "execution_status": "pending",
                    "estimated_impact": "low",
                    "estimated_confidence": 0.9
                }
            ],
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "reasoning": "This pod was identified as having high memory utilization. Restarting the pod should reset memory usage and potentially resolve the issue."
        }
    elif endpoint.startswith("/remediation/status/"):
        # Mock response for remediation status
        diagnosis_id = endpoint.split("/")[-1]
        return {
            "diagnosis_id": diagnosis_id,
            "anomaly_id": mock_anomaly_id_1,
            "status": "in_progress",
            "plan_id": mock_plan_id_1,
            "plan_status": "executing",
            "completed": False,
            "message": "Remediation in progress (mock data)"
        }
    elif endpoint.startswith("/anomalies/") and endpoint.endswith("/remediate"):
        # Mock response for triggering remediation
        anomaly_id = endpoint.split("/")[-2]
        # Check if this is a mock anomaly ID from our own mock anomalies
        if anomaly_id in [mock_anomaly_id_1, mock_anomaly_id_2]:
            # This is one of our mock anomalies, return success
            diagnosis_id = f"mock-diagnosis-{int(time.time())}"
            return {
                "anomaly_id": anomaly_id,
                "status": "initiated",
                "message": "Remediation workflow started (mock data)",
                "diagnosis_id": diagnosis_id
            }
        else:
            # This is not one of our known mock anomalies, simulate a 404 error
            return {
                "error": f"Anomaly ID '{anomaly_id}' not found or cannot be remediated!",
                "status_code": 404
            }
    # Default empty response
    return {"items": [], "error": None}


def get_status_emoji(status: str) -> str:
    """Get appropriate emoji for a status."""
    status = status.lower()
    
    # Handle common status terms with explicit matching
    if status in ["success", "succeeded", "successful", "completed", "complete", "true"]:
        return STATUS_EMOJIS.get("success", "✅")
    elif status in ["fail", "failed", "failure", "error", "false"]:
        return STATUS_EMOJIS.get("failed", "🔴")
    elif status in ["run", "running", "active", "ready"]:
        return STATUS_EMOJIS.get("running", "🟢")
    elif status in ["pending", "waiting", "queued", "in_progress", "progress"]:
        return STATUS_EMOJIS.get("pending", "🟡")
    elif status in ["warn", "warning"]:
        return STATUS_EMOJIS.get("warning", "⚠️")
    elif status in ["info", "information", "notice"]:
        return STATUS_EMOJIS.get("info", "ℹ️")
    
    # Fallback to substring matching
    for key, emoji in STATUS_EMOJIS.items():
        if key in status:
            return emoji
            
    return STATUS_EMOJIS.get("unknown", "❓")


def get_severity_style(score: float) -> str:
    """Get the appropriate style based on severity score (kept for compatibility)."""
    # Simple implementation - not used directly anymore
    return "anomaly.medium"


def display_anomalies(anomalies: List[Dict]):
    """Display a table of detected anomalies with rich formatting and status indicators."""
    if not anomalies:
        console.print(Panel.fit(
            "[warning]No anomalies detected in the cluster.[/warning]",
            title="Cluster Status",
            subtitle="Everything looks healthy!"
        ))
        return

    # Filter out invalid records with validation errors
    valid_anomalies = [a for a in anomalies if not a.get("error") and (a.get("entity_id") or a.get("name"))]
    
    if not valid_anomalies:
        console.print(Panel.fit(
            "[warning]All anomalies have validation errors or missing required fields.[/warning]",
            title="Data Validation Issues",
            subtitle="Check logs for details"
        ))
        return
        
    # Create a more informative table
    table = Table(
        title=f"Detected Anomalies ({len(valid_anomalies)})",
        box=box.ROUNDED,
        highlight=True,
        show_header=True,
        header_style="bold white on blue",
        expand=True,
    )
    
    # Add columns with better descriptions
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center", style="bold")
    table.add_column("Entity", style="green")
    table.add_column("Type", style="yellow")
    table.add_column("Failure", style="red")
    table.add_column("Remediation", style="blue")

    # Sort anomalies by timestamp only (most recent first)
    sorted_anomalies = sorted(
        valid_anomalies, 
        key=lambda a: a.get("detected_at", ""),
        reverse=True
    )

    for anomaly in sorted_anomalies:
        # Make sure we have a valid ID - use anomaly_id field if present
        anomaly_id = anomaly.get("id") or anomaly.get("anomaly_id") or anomaly.get("_id")
        if not anomaly_id or anomaly_id == "Unknown":
            # Generate a unique ID based on entity values if possible
            entity = anomaly.get("entity_id") or ""
            failure = anomaly.get("failure_reason") or ""
            if entity and failure:
                anomaly_id = f"{entity[:8]}-{failure[:8]}"
            else:
                anomaly_id = "A" + str(hash(str(anomaly)) % 10000)
        
        # Get status with emoji
        status = anomaly.get("status", "")
        if not status or status == "Unknown":
            # Try to determine a status value if unknown
            if anomaly.get("remediation_status", "").lower() in ("succeeded", "completed"):
                status = "Resolved"
            elif anomaly.get("active", False):
                status = "Active"
            else:
                status = "Detected"
        status_display = f"{get_status_emoji(status)} {status}"
        
        # Get remediation status
        remediation_status = anomaly.get("remediation_status", "")
        if not remediation_status or remediation_status.lower() == "none":
            remediation_display = "[dim]No action[/dim]"
        elif remediation_status.lower() in ("succeeded", "completed"):
            remediation_display = "[success]Completed[/success]"
        elif remediation_status.lower() in ("failed", "error"):
            remediation_display = "[danger]Failed[/danger]"
        elif remediation_status.lower() in ("in_progress", "running", "diagnosing"):
            remediation_display = "[warning]" + remediation_status.capitalize() + "[/warning]"
        else:
            remediation_display = remediation_status
        
        # Add the row with rich formatting
        table.add_row(
            str(anomaly_id),
            status_display,
            anomaly.get("entity_id", "Unknown"),
            anomaly.get("entity_type", "Unknown"),
            anomaly.get("failure_reason", "Unknown"),
            remediation_display,
        )

    console.print(table)
    
    # Show filtered count if some anomalies were filtered out
    if len(valid_anomalies) < len(anomalies):
        console.print(f"\n[warning]Note: {len(anomalies) - len(valid_anomalies)} anomalies were excluded due to validation errors.[/warning]")
    
    # Add a helpful tip
    console.print(
        "\n[info]💡 Tip: Use '[command]kubewise remediate[/command]' to interactively select an anomaly for remediation[/info]"
    )


def display_remediation_plans(plans: List[Dict]):
    """Display a table of remediation plans with detailed status and actions."""
    if not plans:
        console.print(Panel("[warning]No remediation plans found.[/warning]", 
                           title="Remediation Status"))
        return

    # Create a rich table
    table = Table(
        title=f"Remediation Plans ({len(plans)})",
        box=box.ROUNDED,
        highlight=True,
        show_header=True,
        header_style="bold white on blue",
        expand=True,
    )
    
    # Add columns excluding status
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Entity", style="green")
    table.add_column("Anomaly ID", style="yellow", no_wrap=True)
    table.add_column("Actions", justify="center")
    table.add_column("Created", style="magenta", justify="right")

    # Sort plans by creation time (most recent first)
    sorted_plans = sorted(
        plans, 
        key=lambda p: p.get("created_at", ""),
        reverse=True
    )

    for plan in sorted_plans:
        # Extract plan ID - check multiple possible fields
        plan_id = plan.get("id") or plan.get("_id") or "Unknown"
        
        # Convert ObjectId to string if needed (handles both string and dict representations)
        if isinstance(plan_id, dict) and "$oid" in plan_id:
            plan_id = plan_id.get("$oid", "Unknown")
        
        # Extract anomaly ID and handle possible ObjectId
        anomaly_id = plan.get("anomaly_id", "Unknown")
        if isinstance(anomaly_id, dict) and "$oid" in anomaly_id:
            anomaly_id = anomaly_id.get("$oid", "Unknown")
            
        # Determine the proper status based on multiple fields
        status = plan.get("status", "")
        
        # If status is not explicitly set, derive it from other fields
        if not status:
            if plan.get("completed", False) is True:
                if plan.get("successful", True) is True:  # Default to True if not specified
                    status = "Completed"
                else:
                    status = "Failed"
            else:
                # Check if any actions are in progress
                actions = plan.get("actions", [])
                if actions:
                    # Check both execution_status and status fields in actions
                    action_statuses = [a.get("execution_status", a.get("status", "")).lower() for a in actions]
                    
                    if any(s in ["succeeded", "success", "completed", "complete", "true"] for s in action_statuses) and not all(s in ["succeeded", "success", "completed", "complete", "true"] for s in action_statuses):
                        status = "In Progress"
                    elif all(s in ["succeeded", "success", "completed", "complete", "true"] for s in action_statuses):
                        status = "Completed"
                    elif any(s in ["failed", "fail", "error", "false"] for s in action_statuses):
                        status = "Failed"
                    elif any(s in ["running", "in_progress", "progress", "executing"] for s in action_statuses):
                        status = "In Progress"
                    else:
                        status = "Pending"
                else:
                    status = "Pending"
        
        # Format status with emoji            
        status_display = f"{get_status_emoji(status)} {status}"
                    
        # Format the entity targeted by this plan
        entity_id = plan.get("target_entity_id", "Unknown")
        
        # Get action info
        actions = plan.get("actions", [])
        action_count = len(actions)
        
        # Calculate relative time for created_at
        created_at = plan.get("created_at", "")
        created_time = get_relative_time(created_at) if created_at else "Unknown"
        
        # Format the action count with execution summary if available
        executed_count = 0
        succeeded_count = 0
        failed_count = 0
        
        for action in actions:
            action_status = action.get("execution_status", action.get("status", "")).lower()
            if action.get("executed", False) or action_status not in ["", "pending", "waiting", "queued"]:
                executed_count += 1
            
            if action_status in ["succeeded", "success", "completed", "complete", "true"]:
                succeeded_count += 1
            elif action_status in ["failed", "fail", "error", "false"]:
                failed_count += 1
                
        if executed_count > 0:
            action_summary = f"{executed_count}/{action_count}"
            if succeeded_count > 0:
                action_summary += f" ([success]{succeeded_count}✓[/success])"
            if failed_count > 0:
                action_summary += f" ([danger]{failed_count}✗[/danger])"
        else:
            action_summary = str(action_count)
        
        # Add the row with rich formatting
        table.add_row(
            str(plan_id),
            entity_id,
            str(anomaly_id),
            action_summary,
            created_time,
        )

    console.print(table)


async def poll_remediation_plan(plan_id: str, max_poll_time: int = 60, poll_interval: float = 2.0) -> Dict:
    """
    Poll a remediation plan until it completes or times out.
    
    Args:
        plan_id: ID of the remediation plan to poll
        max_poll_time: Maximum polling time in seconds (default: 60)
        poll_interval: Interval between polls in seconds (default: 2.0)
        
    Returns:
        The final plan data
    """
    start_time = time.time()
    elapsed = 0
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"[cyan]Monitoring plan execution...", total=max_poll_time)
        
        latest_plan = await api_request(f"/remediation/plans/{plan_id}")
        
        while elapsed < max_poll_time:
            # Determine if plan is completed
            status = "Unknown"
            is_completed = False
            
            if latest_plan and isinstance(latest_plan, dict):
                # Check if plan has explicit completed flag
                if latest_plan.get("completed", False) is True:
                    is_completed = True
                
                # Check if all actions have terminal status
                actions = latest_plan.get("actions", [])
                if actions:
                    action_statuses = [a.get("execution_status", a.get("status", "")).lower() for a in actions]
                    
                    # If all actions are in terminal state (succeeded or failed), consider plan completed
                    if all(s in ["succeeded", "success", "completed", "complete", "true", "failed", "fail", "error", "false"] for s in action_statuses):
                        is_completed = True
                        
                    # Determine overall status
                    if any(s in ["failed", "fail", "error", "false"] for s in action_statuses):
                        status = "Failed"
                    elif all(s in ["succeeded", "success", "completed", "complete", "true"] for s in action_statuses):
                        status = "Completed"
                    elif any(s in ["running", "in_progress", "progress", "executing"] for s in action_statuses):
                        status = "In Progress"
                    else:
                        status = "Pending"
                        
                    # Update task description with status
                    progress.update(task, description=f"[cyan]Monitoring plan execution: {status}")
            
            # If plan is completed, break the loop
            if is_completed:
                progress.update(task, description=f"[green]Plan execution completed: {status}")
                break
            
            # Wait for next poll
            await asyncio.sleep(poll_interval)
            
            # Update elapsed time and progress
            elapsed = time.time() - start_time
            progress.update(task, completed=min(elapsed, max_poll_time))
            
            # Fetch latest plan data
            latest_plan = await api_request(f"/remediation/plans/{plan_id}")
    
    # Return the latest plan data
    return latest_plan


async def view_plan_details(plan_id: str, auto_refresh: bool = False):
    """Display detailed information about a remediation plan with rich formatting."""
    with console.status("[info]Fetching plan details...[/info]"):
        plan = await api_request(f"/remediation/plans/{plan_id}")
    
    if isinstance(plan, dict) and "error" in plan:
        console.print(f"[danger]Error fetching plan: {plan['error']}[/danger]")
        return
    
    if not plan:
        console.print(f"[warning]Plan with ID {plan_id} not found[/warning]")
        return

    # Determine if we should poll for updates
    is_completed = plan.get("completed", False)

    # If auto_refresh is enabled and the plan is not completed, poll for updates
    if auto_refresh and not is_completed:
        console.print("[info]Plan execution is in progress. Monitoring status in real-time...[/info]")
        plan = await poll_remediation_plan(plan_id)

    # Get the plan ID - check multiple fields
    display_id = plan.get("id") or plan.get("_id") or plan_id
    
    # Convert ObjectId to string if needed (handles both string and dict representations)
    if isinstance(display_id, dict) and "$oid" in display_id:
        display_id = display_id.get("$oid", plan_id)
    
    # Get anomaly ID and handle possible ObjectId
    anomaly_id = plan.get("anomaly_id", "Unknown")
    if isinstance(anomaly_id, dict) and "$oid" in anomaly_id:
        anomaly_id = anomaly_id.get("$oid", "Unknown")
    
    # Create panel with plan details
    details = [
        f"[bold]Plan ID:[/bold] {display_id}",
        f"[bold]Target Entity:[/bold] {plan.get('target_entity_type', '')} / {plan.get('target_entity_id', 'Unknown')}",
        f"[bold]Anomaly ID:[/bold] {anomaly_id}",
        f"[bold]Created:[/bold] {plan.get('created_at')} ({get_relative_time(plan.get('created_at', ''))})",
    ]
    
    if plan.get("completed", False):
        details.append(f"[bold]Completed:[/bold] {plan.get('completion_time', 'Unknown')} ({get_relative_time(plan.get('completion_time', ''))})")
        success_text = "[success]Successful[/success]" if plan.get("successful", False) else "[danger]Failed[/danger]"
        details.append(f"[bold]Outcome:[/bold] {success_text}")
        
        if plan.get("completion_message"):
            details.append(f"[bold]Completion Message:[/bold] {plan.get('completion_message')}")
    
    # Add diagnosis information if available
    if plan.get("diagnosis_results"):
        details.append("\n[bold]Diagnosis Results:[/bold]")
        for result in plan.get("diagnosis_results", []):
            details.append(f"• {result.get('tool', 'Unknown')}: {result.get('result', 'No result')}")
    
    # Add reasoning with markdown formatting
    if "reasoning" in plan and plan["reasoning"]:
        reasoning = plan.get("reasoning", "No reasoning provided")
        details.append("\n[bold]Remediation Strategy:[/bold]")
        details.append(reasoning)
    
    console.print(Panel.fit(
        "\n".join(details),
        title="Remediation Plan Details",
        subtitle=f"Plan for {plan.get('target_entity_id', 'Unknown')}",
        box=box.ROUNDED,
    ))

    # Display actions with essential details only
    actions = plan.get("actions", [])
    if actions:
        action_table = Table(
            title="Remediation Actions", 
            box=box.ROUNDED,
            highlight=True,
            show_header=True,
            header_style="bold white on blue",
        )
        
        action_table.add_column("#", style="dim", width=3)
        action_table.add_column("Action Type", style="cyan")
        action_table.add_column("Parameters", style="green")

        for idx, action in enumerate(actions, 1):
            # Format parameters for better readability
            params = action.get("parameters", {})
            if params:
                param_lines = []
                for k, v in params.items():
                    if isinstance(v, dict):
                        param_lines.append(f"{k}:")
                        for sub_k, sub_v in v.items():
                            param_lines.append(f"  {sub_k}: {sub_v}")
                    else:
                        param_lines.append(f"{k}: {v}")
                param_text = "\n".join(param_lines)
            else:
                param_text = "None"
            
            action_table.add_row(
                str(idx),
                action.get("action_type", "Unknown"),
                param_text
            )

        console.print(action_table)
    else:
        console.print("[warning]No actions in this plan.[/warning]")
    
    # Provide refresh option for plans that aren't completed
    if not is_completed and not auto_refresh:
        if Confirm.ask("[info]Would you like to monitor this plan in real-time?[/info]"):
            console.clear()
            display_banner()
            await view_plan_details(plan_id, auto_refresh=True)


def get_relative_time(timestamp_str: str) -> str:
    """
    Convert a timestamp string to a human-readable relative time.
    
    Example: "2 hours ago", "5 minutes ago", "just now"
    """
    if not timestamp_str:
        return "Unknown"
        
    try:
        # Try different timestamp formats
        import datetime
        from dateutil import parser
        
        # Parse the timestamp string
        dt = parser.parse(timestamp_str)
        
        # Convert to local timezone if UTC
        if dt.tzinfo is not None and dt.tzinfo.tzname(dt) == 'UTC':
            dt = dt.replace(tzinfo=timezone.utc).astimezone(tz=None)
            
        # Get current time in the same timezone
        now = datetime.datetime.now(dt.tzinfo)
        
        # Calculate the difference
        diff = now - dt
        
        # Format the relative time
        seconds = diff.total_seconds()
        
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
        elif seconds < 86400:
            hours = int(seconds // 3600)
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        elif seconds < 604800:
            days = int(seconds // 86400)
            return f"{days} day{'s' if days > 1 else ''} ago"
        elif seconds < 2592000:
            weeks = int(seconds // 604800)
            return f"{weeks} week{'s' if weeks > 1 else ''} ago"
        else:
            months = int(seconds // 2592000)
            return f"{months} month{'s' if months > 1 else ''} ago"
            
    except (ValueError, TypeError) as e:
        return timestamp_str  # Return the original string if parsing fails


async def trigger_remediation(anomaly_id: str):
    """Manually trigger remediation for a specific anomaly."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task("Triggering remediation...", total=None)
        
        result = await api_request(f"/anomalies/{anomaly_id}/remediate", method="POST")
        progress.update(task, completed=True)
        
        if "error" in result:
            if result.get("status_code") == 404:
                console.print(f"[bold red]Anomaly ID '{anomaly_id}' not found or cannot be remediated![/bold red]")
                console.print("[yellow]The anomaly may have been resolved or deleted, or it might not be eligible for remediation.[/yellow]")
            else:
                console.print("[bold red]Failed to trigger remediation![/bold red]")
                console.print(f"[yellow]Error: {result.get('error', 'Unknown error')}[/yellow]")
            return False
        
        # Extract diagnosis ID and other information from the response
        diagnosis_id = result.get("diagnosis_id")
        status = result.get("status", "initiated")
        message = result.get("message", "Remediation initiated")
        
        console.print(f"[bold green]Successfully triggered remediation for anomaly {anomaly_id}[/bold green]")
        console.print(f"[info]Status: {status}[/info]")
        console.print(f"[info]Message: {message}[/info]")
        
        # If we have a diagnosis ID, offer to track the remediation progress
        if diagnosis_id:
            console.print(f"[info]Diagnosis ID: {diagnosis_id}[/info]")
            
            # Check if user wants to track the remediation process
            if Confirm.ask("[bold cyan]Would you like to monitor the remediation process?[/bold cyan]"):
                # Use our new tracking function which is better suited for following progress
                await track_remediation(diagnosis_id)
        
        return True


# Signal handling for clean shutdown
def setup_signal_handlers():
    """Set up signal handlers for clean shutdown"""
    def signal_handler(sig, frame):
        log.info("Shutdown signal received, cleaning up...")
        cancel_all_tasks()
        console.print("\n[bold red]Shutting down...[/bold red]")
        sys.exit(0)
    
    # Register signal handler for SIGINT (Ctrl+C)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Register signal handler for SIGTERM
    signal.signal(signal.SIGTERM, signal_handler)
    
    log.info("Signal handlers registered.")

def cancel_all_tasks():
    """Cancel all running tasks for clean shutdown"""
    tasks_to_cancel = [t for t in RUNNING_TASKS if not t.done()]
    if not tasks_to_cancel:
        log.info("No running tasks to cancel.")
        return
    
    log.info(f"Cancelling {len(tasks_to_cancel)} running tasks...")
    for task in tasks_to_cancel:
        task.cancel()
    log.info("All tasks cancelled.")

# Modify the watch_logs function to be cancellable
async def watch_logs():
    """Display a live view of KubeWise logs."""
    console.print("[bold]Watching KubeWise logs (press Ctrl+C to stop)...[/bold]")
    
    try:
        # This is a simplified approach - in a real implementation,
        # you might use a WebSocket connection or a file watcher
        with open("kubewise.log", "r") as log_file:
            # Seek to the end of the file
            log_file.seek(0, os.SEEK_END)
            
            # Keep watching for new content
            while not SHUTDOWN_EVENT.is_set():
                line = log_file.readline()
                if line:
                    if "ERROR" in line:
                        console.print(f"[red]{line.strip()}[/red]")
                    elif "WARNING" in line:
                        console.print(f"[yellow]{line.strip()}[/yellow]")
                    elif "INFO" in line:
                        console.print(f"[green]{line.strip()}[/green]")
                    else:
                        console.print(line.strip())
                else:
                    # Use wait_for with a timeout to make it cancellable
                    try:
                        await asyncio.wait_for(SHUTDOWN_EVENT.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
    except KeyboardInterrupt:
        console.print("\n[bold]Stopped watching logs.[/bold]")
    except FileNotFoundError:
        console.print("[bold red]Log file not found.[/bold red]")

# Helper function to create and track tasks
def create_tracked_task(coro, name=None):
    """Create and track an asyncio task for clean shutdown"""
    task = asyncio.create_task(coro, name=name)
    RUNNING_TASKS.add(task)
    task.add_done_callback(lambda t: RUNNING_TASKS.discard(t))
    return task

# Add a new function to apply Kubernetes manifests
async def apply_kubernetes_manifest(manifest_path: str):
    """Apply a Kubernetes manifest using kubectl."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task(f"Applying manifest {manifest_path}...", total=None)
        
        # Run kubectl apply command
        process = await asyncio.create_subprocess_exec(
            "kubectl", "apply", "-f", manifest_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        progress.update(task, completed=True)
        
        if process.returncode != 0:
            console.print(f"[bold red]Failed to apply manifest: {stderr.decode()}[/bold red]")
            return False
        
        console.print(f"[bold green]Successfully applied manifest: {stdout.decode()}[/bold green]")
        return True

# Add a new function to apply Kubernetes manifest content directly
async def apply_kubernetes_manifest_content(manifest_content: str, name: str = "manifest"):
    """Apply a Kubernetes manifest directly from content string instead of a file."""
    import tempfile
    import os
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task(f"Applying {name} manifest...", total=None)
        
        # Create a temporary file with the manifest content
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as temp_file:
            temp_file_path = temp_file.name
            temp_file.write(manifest_content)
        
        try:
            # Run kubectl apply command
            process = await asyncio.create_subprocess_exec(
                "kubectl", "apply", "-f", temp_file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            progress.update(task, completed=True)
            
            if process.returncode != 0:
                console.print(f"[bold red]Failed to apply manifest: {stderr.decode()}[/bold red]")
                return False
            
            console.print(f"[bold green]Successfully applied manifest: {stdout.decode()}[/bold green]")
            return True
        finally:
            # Clean up the temporary file
            try:
                os.unlink(temp_file_path)
            except Exception:
                pass

# Modify the interactive_menu function
async def interactive_menu():
    """Display an interactive menu for the KubeWise CLI."""
    while True:
        display_banner()
        
        console.print("\n[bold cyan]KubeWise Interactive Menu[/bold cyan]")
        console.print("[bold white]------------------------------------------[/bold white]")
        console.print("1. [green]View Detected Anomalies[/green]")
        console.print("2. [yellow]View Remediation Plans[/yellow]")
        console.print("3. [magenta]Trigger Remediation for Anomaly[/magenta]")
        console.print("4. [blue]Check Remediation Status[/blue]")
        console.print("5. [cyan]Watch KubeWise Logs[/cyan]")
        console.print("6. [red]Exit[/red]")
        console.print("[bold white]------------------------------------------[/bold white]")
        
        choice = Prompt.ask("Select an option", choices=["1", "2", "3", "4", "5", "6"])
        
        if choice == "1":
            # View anomalies
            with console.status("[bold green]Fetching anomalies...[/bold green]"):
                anomalies = await api_request("/anomalies")
                
                if isinstance(anomalies, dict) and "error" in anomalies:
                    # Error already displayed by api_request
                    press_to_continue()
                    continue
                
                if isinstance(anomalies, list):
                    display_anomalies(anomalies)
                else:
                    display_anomalies(anomalies.get("items", []))
            
            press_to_continue()
            
        elif choice == "2":
            # View remediation plans
            with console.status("[bold green]Fetching remediation plans...[/bold green]"):
                plans = await api_request("/remediation/plans")
                
                if isinstance(plans, dict) and "error" in plans:
                    # Error already displayed by api_request
                    press_to_continue()
                    continue
                
                if isinstance(plans, list):
                    display_remediation_plans(plans)
                else:
                    display_remediation_plans(plans.get("items", []))
            
            # Ask if they want to see details for a specific plan
            if Confirm.ask("[bold cyan]Would you like to view details for a specific plan?[/bold cyan]"):
                plan_id = Prompt.ask("[bold cyan]Enter the plan ID[/bold cyan]")
                await view_plan_details(plan_id)
                
            press_to_continue()
            
        elif choice == "3":
            # Trigger remediation
            await _interactive_remediation_selection()
            press_to_continue()
            
        elif choice == "4":
            # Check remediation status
            diagnosis_id = Prompt.ask("[bold cyan]Enter the diagnosis ID[/bold cyan]")
            
            with console.status(f"[info]Checking status of diagnosis {diagnosis_id}...[/info]"):
                status_data = await api_request(f"/remediation/status/{diagnosis_id}")
                
            if "error" in status_data:
                console.print(f"[bold red]Error checking status: {status_data.get('error')}[/bold red]")
                press_to_continue()
                continue
            
            # Build a nice panel to display the status
            status_value = status_data.get("status", "unknown")
            message = status_data.get("message", "Status unknown")
            anomaly_id = status_data.get("anomaly_id", "Unknown")
            plan_id = status_data.get("plan_id")
            completed = status_data.get("completed", False)
            
            # Set colors based on status
            if status_value == "completed":
                status_color = "green"
            elif status_value == "failed":
                status_color = "red"
            elif status_value == "in_progress":
                status_color = "cyan"
            elif status_value == "planned":
                status_color = "yellow"
            elif status_value == "diagnosing":
                status_color = "blue"
            else:
                status_color = "white"
            
            details = [
                f"[bold]Diagnosis ID:[/bold] {diagnosis_id}",
                f"[bold]Anomaly ID:[/bold] {anomaly_id}",
                f"[bold]Status:[/bold] [{status_color}]{status_value}[/{status_color}]",
                f"[bold]Message:[/bold] {message}",
            ]
            
            if plan_id:
                details.append(f"[bold]Remediation Plan ID:[/bold] {plan_id}")
                details.append(f"[bold]Plan Status:[/bold] {status_data.get('plan_status', 'Unknown')}")
                
            console.print(Panel.fit(
                "\n".join(details),
                title="Remediation Status",
                box=box.ROUNDED,
            ))
            
            # If not completed, ask if they want to monitor
            if not completed:
                if Confirm.ask("[bold cyan]Would you like to monitor this remediation in real-time?[/bold cyan]"):
                    await track_remediation(diagnosis_id)
            
            # If there's a plan, ask if they want to view details
            if plan_id:
                if Confirm.ask("[bold cyan]Would you like to see the remediation plan details?[/bold cyan]"):
                    asyncio.run(view_plan_details(plan_id))
            
            press_to_continue()
            
        elif choice == "5":
            # Watch logs
            try:
                await watch_logs()
            except KeyboardInterrupt:
                pass
            
            press_to_continue()
            
        elif choice == "6":
            # Exit
            console.print("[bold green]Thank you for using KubeWise! Goodbye![/bold green]")
            sys.exit(0)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    mock: bool = typer.Option(False, "--mock", help="Use mock data for testing"),
    install_completion: bool = typer.Option(
        False, "--install-completion", help="Install completion for the current shell."
    ),
    show_completion: bool = typer.Option(
        False, "--show-completion", help="Show completion for the current shell, to copy it or customize the installation."
    ),
):
    """
    KubeWise CLI - Interactive interface for monitoring and remediation.
    
    This CLI provides commands to interact with the KubeWise API for demo purposes,
    focusing on CrashLoopBackOff and ImagePullBackOff failures.
    """
    global MOCK_MODE
    MOCK_MODE = mock
    
    if install_completion:
        _install_completion()
    if show_completion:
        _show_completion()
    
    # If no command was invoked, show the interactive menu if no other flags were used
    if ctx.invoked_subcommand is None and not (install_completion or show_completion):
        asyncio.run(interactive_menu())


@app.command()
def interactive(
    mock: bool = typer.Option(False, "--mock", help="Use mock data for testing"),
):
    """Launch the interactive KubeWise CLI menu."""
    try:
        global MOCK_MODE
        MOCK_MODE = mock
        
        asyncio.run(interactive_menu())
    except KeyboardInterrupt:
        console.print("\n[bold green]Thank you for using KubeWise! Goodbye![/bold green]")
        sys.exit(0)


@app.command()
def anomalies(
    mock: bool = typer.Option(False, "--mock", help="Use mock data for testing"),
):
    """List all detected anomalies."""
    try:
        global MOCK_MODE
        MOCK_MODE = mock
        
        display_banner()
        anomalies_data = asyncio.run(api_request("/anomalies"))
        
        if isinstance(anomalies_data, dict) and "error" in anomalies_data:
            # Error already displayed by api_request
            return
        
        if isinstance(anomalies_data, list):
            display_anomalies(anomalies_data)
        else:
            display_anomalies(anomalies_data.get("items", []))
    except KeyboardInterrupt:
        console.print("\n[bold]Operation cancelled.[/bold]")


@app.command()
def plans(
    plan_id: Optional[str] = typer.Option(None, "--id", help="View details for a specific plan ID"),
    auto_refresh: bool = typer.Option(False, "--monitor", "-m", help="Monitor plan execution in real-time"),
    mock: bool = typer.Option(False, "--mock", help="Use mock data for testing"),
):
    """List all remediation plans or view details for a specific plan."""
    try:
        global MOCK_MODE
        MOCK_MODE = mock
        
        display_banner()
        
        # If a specific plan ID is provided, show its details
        if plan_id:
            asyncio.run(view_plan_details(plan_id, auto_refresh=auto_refresh))
            return
            
        # Otherwise list all plans
        plans_data = asyncio.run(api_request("/remediation/plans"))
        
        if isinstance(plans_data, dict) and "error" in plans_data:
            # Error already displayed by api_request
            return
            
        if isinstance(plans_data, list):
            # Handle response as a list of plans directly
            display_remediation_plans(plans_data)
        else:
            # Handle response as a dict with items array
            display_remediation_plans(plans_data.get("items", []))
    except KeyboardInterrupt:
        console.print("\n[bold]Operation cancelled.[/bold]")


@app.command()
def remediate(
    anomaly_id: Optional[str] = typer.Option(None, "--id", help="ID of the anomaly to remediate"),
    mock: bool = typer.Option(False, "--mock", help="Use mock data for testing"),
):
    """Trigger remediation for a specific anomaly. If no ID is provided, shows a list of active anomalies to choose from."""
    try:
        global MOCK_MODE
        MOCK_MODE = mock
        
        display_banner()
        
        # If no anomaly_id is provided, fetch active anomalies and let user select
        if not anomaly_id:
            # Use the event loop to run the async function
            asyncio.run(_interactive_remediation_selection())
        else:
            # Otherwise, remediate the specified anomaly
            asyncio.run(trigger_remediation(anomaly_id))
    except KeyboardInterrupt:
        console.print("\n[bold]Operation cancelled.[/bold]")


@app.command()
def logs(
    mock: bool = typer.Option(False, "--mock", help="Use mock data for testing"),
):
    """Display live KubeWise logs."""
    try:
        global MOCK_MODE
        MOCK_MODE = mock
        
        display_banner()
        asyncio.run(watch_logs())
    except KeyboardInterrupt:
        console.print("\n[bold]Operation cancelled.[/bold]")


@app.command(hidden=True)
def show_completion_script(shell: str = typer.Argument("bash")):
    """
    Show completion script for the specified shell.
    This command is hidden and used internally by the completion system.
    """
    console.print("plain,anomalies")
    console.print("plain,plans")
    console.print("plain,interactive")
    console.print("plain,remediate")
    console.print("plain,logs")
    console.print("plain,--help")
    console.print("plain,--mock")
    sys.exit(0)


def _install_completion():
    """Install completion for the current shell."""
    shell = os.environ.get("SHELL", "")
    shell_name = os.path.basename(shell).lower()
    
    if not shell:
        console.print("[bold red]Cannot detect shell type. Please specify manually.[/bold red]")
        return
    
    completion_script = _get_completion_script()
    
    if "bash" in shell_name:
        completion_file = Path.home() / ".bash_completion"
        if not completion_file.exists():
            completion_file.touch()
        
        with open(completion_file, "a") as f:
            f.write(f"\n{completion_script}\n")
        
        console.print(f"[bold green]Bash completion installed to {completion_file}[/bold green]")
        console.print("[yellow]Please restart your shell or run 'source ~/.bash_completion'[/yellow]")
    
    elif "zsh" in shell_name:
        completion_dir = Path.home() / ".zsh/completion"
        completion_dir.mkdir(parents=True, exist_ok=True)
        completion_file = completion_dir / "_kubewise"
        
        with open(completion_file, "w") as f:
            f.write(completion_script)
        
        zshrc = Path.home() / ".zshrc"
        completion_line = 'fpath=($HOME/.zsh/completion $fpath)'
        autoload_line = 'autoload -Uz compinit && compinit'
        
        # Check if the lines are already in .zshrc
        add_completion = True
        add_autoload = True
        
        if zshrc.exists():
            with open(zshrc, "r") as f:
                content = f.read()
                if completion_line in content:
                    add_completion = False
                if autoload_line in content:
                    add_autoload = False
        
        with open(zshrc, "a") as f:
            f.write("\n# Added by KubeWise installer\n")
            if add_completion:
                f.write(f"{completion_line}\n")
            if add_autoload:
                f.write(f"{autoload_line}\n")
        
        console.print(f"[bold green]Zsh completion installed to {completion_file}[/bold green]")
        console.print("[yellow]Please restart your shell or run 'source ~/.zshrc'[/yellow]")
    
    else:
        console.print(f"[bold red]Shell '{shell_name}' is not supported for automatic completion installation.[/bold red]")
        console.print("[yellow]Here's the completion script if you want to install it manually:[/yellow]")
        console.print(completion_script)
    
    sys.exit(0)


def _show_completion():
    """Show completion for the current shell, to copy it or customize the installation."""
    console.print(_get_completion_script())
    sys.exit(0)


def _get_completion_script():
    """Generate shell completion script."""
    prog_name = "kubewise"
    
    # Create a bash/zsh completion script
    script = f"""
# kubewise completion script
_{prog_name}_completion() {{
    local IFS=$'\\n'
    local response
    
    response=$(env COMP_WORDS="${{COMP_WORDS[*]}}" COMP_CWORD=$COMP_CWORD {prog_name} show-completion-script 2>/dev/null)
    
    for completion in $response; do
        IFS=',' read type value <<< "$completion"
        if [[ $type == 'dir' ]]; then
            COMPREPLY=( $(compgen -d -- "$value") )
            return
        elif [[ $type == 'file' ]]; then
            COMPREPLY=( $(compgen -f -- "$value") )
            return
        elif [[ $type == 'plain' ]]; then
            COMPREPLY+=("$value")
        fi
    done
}}

complete -o nospace -F _{prog_name}_completion {prog_name}
"""
    return script

# Modify run_app to set up signal handlers before running the app
def run_app():
    """Run the application with proper signal handling"""
    setup_signal_handlers()
    app()

async def _interactive_remediation_selection():
    """Interactive helper for selecting an anomaly to remediate"""
    with console.status("[bold green]Fetching active anomalies...[/bold green]"):
        anomalies_data = await api_request("/anomalies")
        
        if isinstance(anomalies_data, dict) and "error" in anomalies_data:
            # Error already displayed by api_request
            return
        
        # Extract anomalies list
        anomalies_list = []
        if isinstance(anomalies_data, list):
            anomalies_list = anomalies_data
        else:
            anomalies_list = anomalies_data.get("items", [])
        
        # Filter for valid anomalies (exclude those with validation errors)
        valid_anomalies = [
            a for a in anomalies_list 
            if not a.get("error") and (a.get("entity_id") or a.get("name"))
        ]
        
        # Then filter for active anomalies (those without completed remediation)
        active_anomalies = [
            a for a in valid_anomalies 
            if a.get("remediation_status", "").lower() not in ["completed", "succeeded"]
        ]
        
        if not active_anomalies:
            console.print("[warning]No active anomalies found that need remediation.[/warning]")
            if len(valid_anomalies) < len(anomalies_list):
                console.print(f"[info]Note: {len(anomalies_list) - len(valid_anomalies)} anomalies were excluded due to validation errors.[/info]")
            return
    
    # Display active anomalies
    console.print("\n[bold cyan]Active Anomalies:[/bold cyan]")
    
    table = Table(
        title="Select an Anomaly to Remediate",
        box=box.ROUNDED,
        highlight=True,
        show_header=True,
        header_style="bold white on blue",
    )
    
    table.add_column("#", style="dim", width=3)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Entity", style="green")
    table.add_column("Failure", style="red")
    
    # Save anomaly IDs in a list for proper selection
    anomaly_ids = []
    
    # Add anomalies to the table with a selection number
    for idx, anomaly in enumerate(active_anomalies, 1):
        anomaly_id = anomaly.get("id") or anomaly.get("anomaly_id") or anomaly.get("_id", "Unknown")
        
        # Store the anomaly ID for later selection
        anomaly_ids.append(anomaly_id)
        
        table.add_row(
            str(idx),
            str(anomaly_id),
            anomaly.get("entity_id", "Unknown"),
            anomaly.get("failure_reason", "Unknown"),
        )
    
    console.print(table)
    
    # Ask user to select an anomaly
    selection = Prompt.ask(
        "Select anomaly to remediate (number) or enter anomaly ID directly",
        default="1"
    )
    
    # Determine the anomaly ID based on selection
    anomaly_id = None
    try:
        # Check if the input is a number (selection from the list)
        idx = int(selection) - 1
        if 0 <= idx < len(anomaly_ids):
            anomaly_id = anomaly_ids[idx]
        else:
            console.print("[bold red]Invalid selection number.[/bold red]")
            return
    except ValueError:
        # If not a number, assume it's an ID entered directly
        anomaly_id = selection
    
    # Trigger remediation for the selected anomaly
    if anomaly_id:
        console.print(f"[info]Triggering remediation for anomaly: {anomaly_id}[/info]")
        await trigger_remediation(anomaly_id)
    else:
        console.print("[bold red]No valid anomaly ID selected.[/bold red]")

async def track_remediation(diagnosis_id: str, max_poll_time: int = 300, poll_interval: float = 5.0):
    """
    Track a remediation process by diagnosis ID until it completes or times out.
    
    Args:
        diagnosis_id: ID of the diagnosis to track
        max_poll_time: Maximum polling time in seconds (default: 300 - 5 minutes)
        poll_interval: Interval between polls in seconds (default: 5.0)
    """
    start_time = time.time()
    elapsed = 0
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(f"[cyan]Tracking remediation process...", total=max_poll_time)
        
        while elapsed < max_poll_time:
            # Get current remediation status
            status_data = await api_request(f"/remediation/status/{diagnosis_id}")
            
            if "error" in status_data:
                progress.update(task, completed=max_poll_time, description=f"[red]Error checking remediation: {status_data.get('error')}")
                break
            
            status = status_data.get("status", "unknown")
            message = status_data.get("message", "Status unknown")
            plan_id = status_data.get("plan_id")
            completed = status_data.get("completed", False)
            
            # Update progress with current status
            if status == "completed":
                progress.update(task, completed=max_poll_time, description=f"[green]Remediation complete: {message}")
                break
            elif status == "failed":
                progress.update(task, completed=max_poll_time, description=f"[red]Remediation failed: {message}")
                break
            elif status == "in_progress":
                progress.update(task, completed=elapsed, description=f"[cyan]Remediation in progress: {message}")
            elif status == "planned":
                progress.update(task, completed=elapsed, description=f"[yellow]Remediation planned: {message}")
            elif status == "diagnosing":
                progress.update(task, completed=elapsed, description=f"[blue]Diagnosing issue: {message}")
            else:
                progress.update(task, completed=elapsed, description=f"[dim]Status {status}: {message}")
            
            # If completed, break the loop
            if completed:
                break
                
            # Wait for next poll
            await asyncio.sleep(poll_interval)
            
            # Update elapsed time
            elapsed = time.time() - start_time
            progress.update(task, completed=min(elapsed, max_poll_time))
    
    # After tracking completes, check if there's a plan to view
    final_status = await api_request(f"/remediation/status/{diagnosis_id}")
    
    if "error" not in final_status and final_status.get("plan_id"):
        plan_id = final_status.get("plan_id")
        
        # Ask if user wants to see the plan details
        if Confirm.ask(f"\n[bold cyan]Would you like to see details of the remediation plan?[/bold cyan]"):
            asyncio.run(view_plan_details(plan_id))

@app.command()
def status(
    diagnosis_id: str = typer.Argument(..., help="ID of the diagnosis to check"),
    monitor: bool = typer.Option(False, "--monitor", "-m", help="Monitor the remediation process in real-time"),
    mock: bool = typer.Option(False, "--mock", help="Use mock data for testing"),
):
    """Check the status of a remediation process by diagnosis ID."""
    try:
        global MOCK_MODE
        MOCK_MODE = mock
        
        display_banner()
        
        if monitor:
            # Monitor the remediation process in real-time
            asyncio.run(track_remediation(diagnosis_id))
        else:
            # Just show current status
            with console.status(f"[info]Checking status of diagnosis {diagnosis_id}...[/info]"):
                status_data = asyncio.run(api_request(f"/remediation/status/{diagnosis_id}"))
                
            if "error" in status_data:
                console.print(f"[bold red]Error checking status: {status_data.get('error')}[/bold red]")
                return
            
            # Build a nice panel to display the status
            status_value = status_data.get("status", "unknown")
            message = status_data.get("message", "Status unknown")
            anomaly_id = status_data.get("anomaly_id", "Unknown")
            plan_id = status_data.get("plan_id")
            completed = status_data.get("completed", False)
            
            # Set colors based on status
            if status_value == "completed":
                status_color = "green"
            elif status_value == "failed":
                status_color = "red"
            elif status_value == "in_progress":
                status_color = "cyan"
            elif status_value == "planned":
                status_color = "yellow"
            elif status_value == "diagnosing":
                status_color = "blue"
            else:
                status_color = "white"
            
            details = [
                f"[bold]Diagnosis ID:[/bold] {diagnosis_id}",
                f"[bold]Anomaly ID:[/bold] {anomaly_id}",
                f"[bold]Status:[/bold] [{status_color}]{status_value}[/{status_color}]",
                f"[bold]Message:[/bold] {message}",
            ]
            
            if plan_id:
                details.append(f"[bold]Remediation Plan ID:[/bold] {plan_id}")
                details.append(f"[bold]Plan Status:[/bold] {status_data.get('plan_status', 'Unknown')}")
                
            console.print(Panel.fit(
                "\n".join(details),
                title="Remediation Status",
                box=box.ROUNDED,
            ))
            
            # If not completed and interactive mode, ask if they want to monitor
            if not completed and not monitor:
                if Confirm.ask("[bold cyan]Would you like to monitor this remediation in real-time?[/bold cyan]"):
                    asyncio.run(track_remediation(diagnosis_id))
            
            # If there's a plan, ask if they want to view details
            if plan_id:
                if Confirm.ask("[bold cyan]Would you like to see the remediation plan details?[/bold cyan]"):
                    asyncio.run(view_plan_details(plan_id))
                
    except KeyboardInterrupt:
        console.print("\n[bold]Operation cancelled.[/bold]")

@app.command()
def logging(
    component: str = typer.Argument(
        "kubewise", help="Component name (e.g., kubewise.collector, kubewise.api)"
    ),
    level: str = typer.Option(
        "INFO", help="Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    ),
    group_window: Optional[float] = typer.Option(
        None, "--group", "-g", help="Time window (seconds) to group similar log messages"
    ),
):
    """
    Adjust logging verbosity and grouping for specific components.
    
    Examples:
        kubewise logging kubewise DEBUG       # Set root logger to DEBUG
        kubewise logging kubewise.collector INFO    # Set collector to INFO
        kubewise logging --group 10           # Group similar logs within 10 second window
        kubewise logging kubewise.remediation.diagnosis WARNING  # Set diagnosis to WARNING
    """
    from kubewise.logging import set_component_log_level, set_log_grouping_window
    
    # Handle log grouping time window update
    if group_window is not None:
        try:
            set_log_grouping_window(group_window)
            console.print(f"[success]Set log grouping window to {group_window} seconds[/success]")
        except Exception as e:
            console.print(f"[danger]Error setting log grouping window: {e}[/danger]")
    
    # Handle component log level update
    if level.upper() not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
        console.print(f"[danger]Invalid log level: {level}[/danger]")
        console.print("Valid levels: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return
    
    try:
        set_component_log_level(component, level.upper())
        console.print(f"[success]Set {component} log level to {level.upper()}[/success]")
    except Exception as e:
        console.print(f"[danger]Error setting log level: {e}[/danger]")

@app.command()
def log_stats(
    reset: bool = typer.Option(
        False, "--reset", "-r", help="Reset log statistics after displaying them"
    ),
):
    """
    Display statistics about log grouping and message consolidation.
    
    Shows how many log messages have been grouped together to reduce noise.
    """
    from kubewise.logging import get_log_group_stats, reset_log_grouping_stats
    
    stats = get_log_group_stats()
    
    console.print(f"\n[bold]Log Grouping Statistics:[/bold]")
    console.print(f"Grouping window: [cyan]{stats['window_seconds']}[/cyan] seconds")
    console.print(f"Total message groups: [cyan]{stats['total_groups']}[/cyan]")
    console.print(f"Total grouped messages: [cyan]{stats['total_grouped_messages']}[/cyan]")
    
    if stats['top_grouped']:
        console.print("\n[bold]Top grouped messages:[/bold]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Component", style="cyan")
        table.add_column("Level", style="yellow")
        table.add_column("Count", style="green", justify="right")
        table.add_column("Message")
        
        for item in stats['top_grouped']:
            table.add_row(
                item['component'], 
                item['level'], 
                str(item['count']), 
                item['message'][:60] + ("..." if len(item['message']) > 60 else "")
            )
        
        console.print(table)
    else:
        console.print("\nNo grouped messages found.")
        
    if reset:
        reset_log_grouping_stats()
        console.print("\n[success]Log statistics have been reset[/success]")

@app.command()
def metrics_watch(
    timeout: int = typer.Option(60, "--timeout", "-t", help="How long to watch metrics in seconds"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only show summary, not individual metrics"),
    real: bool = typer.Option(False, "--real", "-r", help="Show real metrics from Prometheus instead of simulated data"),
):
    """
    Watch metrics collection with a visual spinner instead of verbose logs.
    
    Shows real-time collection progress without filling the console with debug messages.
    """
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    import time
    import signal
    import asyncio
    
    # Set up a graceful exit handler
    should_exit = False
    
    def handle_exit(signum, frame):
        nonlocal should_exit
        should_exit = True
        console.print("\n[yellow]Stopping metrics watch...[/yellow]")
    
    # Register signal handler
    signal.signal(signal.SIGINT, handle_exit)
    
    console.print("[bold]Starting metrics watch...[/bold]")
    console.print("Press Ctrl+C to exit")
    
    # Configure lowered log level for metrics collection
    from kubewise.logging import set_component_log_level, set_log_grouping_window
    
    # Set a larger grouping window to further reduce noise
    set_log_grouping_window(30.0)  # Group similar logs within 30 seconds
    
    # Make collector logs quieter
    set_component_log_level("kubewise.collector.prometheus", "WARNING" if quiet else "INFO")
    
    if real:
        # Use the real metrics collector
        async def watch_real_metrics():
            # Import necessary components
            import httpx
            import asyncio
            from kubewise.collector.prometheus import PrometheusFetcher
            from kubewise.config import settings
            
            # Create metrics queue
            metrics_queue = asyncio.Queue()
            
            # Create HTTP client
            async with httpx.AsyncClient() as client:
                # Create metrics fetcher
                fetcher = PrometheusFetcher(
                    metrics_queue=metrics_queue,
                    http_client=client,
                    prometheus_url=str(settings.prom_url)
                )
                
                # Start the fetcher
                await fetcher.start()
                
                start_time = time.time()
                metrics_count = 0
                poll_count = 0
                
                try:
                    while time.time() - start_time < timeout and not should_exit:
                        # Use visual loader for metrics collection
                        metrics = await fetcher._fetch_all_metrics_with_loader()
                        metrics_count += len(metrics)
                        poll_count += 1
                        
                        # Wait a bit before next poll
                        await asyncio.sleep(1)
                finally:
                    # Stop the fetcher
                    await fetcher.stop()
                
                return metrics_count, poll_count, time.time() - start_time
        
        # Run the async function
        metrics_count, poll_count, elapsed = asyncio.run(watch_real_metrics())
        
        # Print a summary
        console.print(f"\n[bold green]Metrics Watch Complete:[/bold green]")
        console.print(f"- Total time: {elapsed:.1f} seconds")
        console.print(f"- Poll cycles: {poll_count}")
        console.print(f"- Metrics collected: {metrics_count}")
        console.print(f"- Collection rate: {metrics_count / elapsed:.1f} metrics/second")
        
    else:
        # Simulated metrics with a progress bar
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TextColumn("[bold]{task.fields[metrics_count]}"),
            TimeElapsedColumn(),
            expand=True,
        ) as progress:
            # Add the main task
            task = progress.add_task(
                "[green]Simulating metrics collection...", 
                total=timeout,
                metrics_count="0 metrics collected"
            )
            
            start_time = time.time()
            metrics_count = 0
            elapsed = 0
            
            # Main loop
            while elapsed < timeout and not should_exit:
                # Update the progress bar
                progress.update(task, completed=elapsed, refresh=True)
                
                # Simulate metrics collection
                metrics_count += 30  # Increment by typical batch size
                
                # Update display
                progress.update(
                    task, 
                    description=f"[green]Collecting metrics... ({metrics_count / elapsed:.1f}/s)",
                    metrics_count=f"{metrics_count} metrics collected"
                )
                
                # Sleep briefly
                time.sleep(1)
                elapsed = time.time() - start_time
            
            # Complete the task
            progress.update(task, completed=timeout, refresh=True)
        
        # Print a summary
        console.print(f"\n[bold green]Metrics Watch Complete:[/bold green]")
        console.print(f"- Total time: {elapsed:.1f} seconds")
        console.print(f"- Metrics collected: {metrics_count} (simulated)")
        console.print(f"- Collection rate: {metrics_count / elapsed:.1f} metrics/second")
    
    # Reset log levels
    set_component_log_level("kubewise.collector.prometheus", "INFO")
    set_log_grouping_window(5.0)  # Reset to default

if __name__ == "__main__":
    run_app() 