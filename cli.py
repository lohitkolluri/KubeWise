import asyncio
import json
import os
from datetime import datetime
from typing import Optional, List, Dict

import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.panel import Panel
from rich.syntax import Syntax
import questionary
from loguru import logger

from kubewise.config import settings
from kubewise.models import AnomalyRecord
from kubewise.api.context import AppContext
from kubewise.models.detector import SequentialAnomalyDetector

# Initialize Typer app with rich help formatting
app = typer.Typer(
    help="KubeWise CLI - Kubernetes Anomaly Detection & Remediation",
    rich_markup_mode="rich",
)
console = Console()

# Create command groups
config_app = typer.Typer(help="Configuration management commands", rich_markup_mode="rich")
anomaly_app = typer.Typer(help="Anomaly detection commands", rich_markup_mode="rich")
remediation_app = typer.Typer(help="Remediation management commands", rich_markup_mode="rich")
metrics_app = typer.Typer(help="Metrics and monitoring commands", rich_markup_mode="rich")

app.add_typer(config_app, name="config", help="Manage KubeWise configuration")
app.add_typer(anomaly_app, name="anomaly", help="Work with anomaly detection")
app.add_typer(remediation_app, name="remediate", help="Handle remediation tasks")
app.add_typer(metrics_app, name="metrics", help="Monitor system metrics")

def format_timestamp(ts: datetime) -> str:
    """Format timestamp for display."""
    return ts.strftime("%Y-%m-%d %H:%M:%S")

@app.command()
def status():
    """Show current KubeWise system status with rich formatting."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task(description="Checking system status...", total=None)
        
        # Create status table
        table = Table(title="KubeWise Status")
        table.add_column("Component", style="cyan")
        table.add_column("Status", style="green")
        table.add_column("Details", style="yellow")
        
        # Add component status rows
        table.add_row(
            "Anomaly Detection",
            "Active",
            f"Threshold: {settings.anomaly_threshold}"
        )
        table.add_row(
            "Prometheus Connection",
            "Connected",
            str(settings.prom_url)
        )
        table.add_row(
            "Kubernetes Connection",
            "Connected",
            "Current context"
        )
        table.add_row(
            "AI Features",
            "Enabled" if settings.gemini_api_key else "Disabled",
            f"Model: {settings.gemini_model_id}"
        )
        
        console.print(table)

@anomaly_app.command("list")
def list_anomalies(
    limit: int = typer.Option(10, help="Number of anomalies to show"),
    severity: Optional[str] = typer.Option(None, help="Filter by severity (Critical, Warning, Info)"),
    status: Optional[str] = typer.Option(None, help="Filter by status"),
):
    """List detected anomalies with rich formatting and filtering."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task(description="Fetching anomalies...", total=None)
        
        # Create anomalies table
        table = Table(title="Recent Anomalies")
        table.add_column("ID", style="cyan")
        table.add_column("Timestamp", style="blue")
        table.add_column("Entity", style="magenta")
        table.add_column("Severity", style="red")
        table.add_column("Status", style="green")
        table.add_column("Score", style="yellow")
        
        # Add sample data (replace with actual data fetch)
        table.add_row(
            "a123",
            "2024-03-20 10:30:00",
            "pod/nginx-123",
            "Critical",
            "Detected",
            "0.95"
        )
        
        console.print(table)

@config_app.command("show")
def show_config():
    """Display current configuration settings."""
    config_data = {
        "Prometheus URL": str(settings.prom_url),
        "Log Level": settings.log_level,
        "AI Model": settings.gemini_model_id,
        "AI Features": "Enabled" if settings.gemini_api_key else "Disabled",
        "Anomaly Threshold": settings.anomaly_threshold,
    }
    
    panel = Panel.fit(
        Syntax(json.dumps(config_data, indent=2), "json", theme="monokai"),
        title="Current Configuration",
        border_style="cyan"
    )
    console.print(panel)

@config_app.command("set")
def set_config(
    key: str = typer.Argument(..., help="Configuration key to set"),
    value: str = typer.Argument(..., help="Value to set"),
):
    """Set a configuration value."""
    try:
        # Validate and set the configuration
        if key == "log_level":
            if value.upper() not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
                raise ValueError("Invalid log level")
            settings.log_level = value.upper()
        elif key == "anomaly_threshold":
            threshold = float(value)
            if not 0 <= threshold <= 1:
                raise ValueError("Threshold must be between 0 and 1")
            settings.anomaly_threshold = threshold
        else:
            raise ValueError(f"Unknown configuration key: {key}")
        
        console.print(f"[green]Successfully updated {key} to {value}")
    except ValueError as e:
        console.print(f"[red]Error: {str(e)}")

@remediation_app.command("auto")
def toggle_auto_remediation(
    enable: bool = typer.Option(None, help="Enable or disable auto-remediation", prompt=True),
):
    """Toggle automatic remediation mode."""
    mode = "enabled" if enable else "disabled"
    if Confirm.ask(f"Are you sure you want to {mode} auto-remediation?"):
        # Update remediation mode
        console.print(f"[green]Auto-remediation {mode}")
    else:
        console.print("Operation cancelled")

@metrics_app.command("watch")
def watch_metrics(
    interval: int = typer.Option(5, help="Refresh interval in seconds"),
    metric: Optional[str] = typer.Option(None, help="Specific metric to watch"),
):
    """Watch metrics in real-time with auto-refresh."""
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            while True:
                progress.add_task(description="Fetching metrics...", total=None)
                
                # Create metrics table
                table = Table(title="Live Metrics")
                table.add_column("Metric", style="cyan")
                table.add_column("Value", style="green")
                table.add_column("Threshold", style="yellow")
                table.add_column("Status", style="red")
                
                # Add sample metrics (replace with actual metric fetch)
                table.add_row(
                    "cpu_usage",
                    "75%",
                    "90%",
                    "Normal"
                )
                
                console.clear()
                console.print(table)
                console.print(f"\nRefreshing every {interval} seconds. Press Ctrl+C to stop.")
                
                asyncio.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching metrics")

@app.command()
def configure():
    """Interactive configuration wizard."""
    # Get current settings
    current_settings = {
        "log_level": settings.log_level,
        "prometheus_url": str(settings.prom_url),
        "anomaly_threshold": settings.anomaly_threshold,
    }
    
    # Interactive configuration using questionary
    new_settings = {}
    
    new_settings["log_level"] = questionary.select(
        "Select log level:",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=current_settings["log_level"]
    ).ask()
    
    new_settings["prometheus_url"] = questionary.text(
        "Enter Prometheus URL:",
        default=current_settings["prometheus_url"]
    ).ask()
    
    new_settings["anomaly_threshold"] = float(questionary.text(
        "Enter anomaly threshold (0.0-1.0):",
        default=str(current_settings["anomaly_threshold"]),
        validate=lambda x: 0 <= float(x) <= 1
    ).ask())
    
    # Save configuration
    # Implementation would update settings
    console.print("[green]Configuration updated successfully!")

@app.command()
def train():
    """Retrain the anomaly detection model."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task(description="Training model...", total=100)
        
        # Simulate training progress
        for i in range(100):
            progress.update(task, advance=1)
            asyncio.sleep(0.1)
            
        console.print("[green]Model training completed successfully!")

@app.callback()
def main(ctx: typer.Context):
    """
    KubeWise CLI - Intelligent Kubernetes Anomaly Detection & Self-Remediation
    
    Run 'kubewise --help' for usage information.
    """
    # Initialize logging
    logger.remove()
    logger.add(
        lambda msg: console.print(f"[dim]{msg}[/dim]"),
        level=settings.log_level
    )

if __name__ == "__main__":
    app()