import asyncio
import datetime # Added import
from typing import Optional

import httpx
import motor.motor_asyncio
import typer
from rich import print as rprint
from rich.table import Table

from kubewise.api.deps import get_mongo_db # Reuse dependency logic structure conceptually
from kubewise.collector import k8s_events, prometheus
from kubewise.config import settings
from kubewise.logging import setup_logging # Configure logging for CLI use
from kubewise.models import AnomalyRecord

# Initialize Typer app
app = typer.Typer(
    name="kubewise",
    help="CLI for interacting with the KubeWise Anomaly Detection System.",
    add_completion=False,
)

# --- Helper Functions (Async Context) ---

async def _get_db():
    """Helper to get DB connection for CLI commands."""
    try:
        client = motor.motor_asyncio.AsyncIOMotorClient(
            str(settings.mongo_uri), serverSelectionTimeoutMS=3000
        )
        await client.admin.command('ping')
        db = client.get_database()
        return client, db # Return client too for closing
    except Exception as e:
        rprint(f"[bold red]Error connecting to MongoDB:[/bold red] {e}")
        return None, None

# --- CLI Commands ---

@app.command()
def config():
    """
    Display the current KubeWise configuration (secrets masked).
    """
    setup_logging() # Ensure logs are configured if needed by settings access
    rprint("[bold cyan]--- KubeWise Configuration ---[/bold cyan]")
    rprint(f"[green]MongoDB URI Set:[/green] {bool(settings.mongo_uri)}")
    rprint(f"[green]Prometheus URL:[/green] {settings.prom_url}")
    gemini_set = bool(settings.gemini_api_key.get_secret_value() != "changeme" and settings.gemini_api_key.get_secret_value())
    rprint(f"[green]Gemini API Key Set:[/green] {gemini_set}")
    rprint(f"[green]Anomaly Threshold:[/green] {settings.anomaly_threshold}")
    rprint(f"[green]Log Level:[/green] {settings.log_level}")
    rprint(f"[green]Gemini Model ID:[/green] {settings.gemini_model_id}")
    rprint("[bold cyan]-----------------------------[/bold cyan]")

@app.command()
def check_connections():
    """
    Perform basic checks for connectivity to Prometheus and Kubernetes API.
    """
    setup_logging() # Configure logging for output

    async def checker():
        # Check Prometheus
        rprint("[cyan]Checking Prometheus connection...[/cyan]")
        async with httpx.AsyncClient() as http_client:
            try:
                test_query = "prometheus_build_info"
                data = await prometheus.query_prometheus(http_client, test_query)
                if data:
                    rprint("[bold green]Prometheus connection successful.[/bold green]")
                else:
                    rprint(f"[bold red]Prometheus connection failed. Check URL: {settings.prom_url}[/bold red]")
            except Exception as e:
                rprint(f"[bold red]Error connecting to Prometheus:[/bold red] {e}")

        # Check Kubernetes
        rprint("\n[cyan]Checking Kubernetes connection...[/cyan]")
        try:
            await k8s_events.load_k8s_config()
            # Optionally perform a simple API call, e.g., list namespaces
            # async with client.ApiClient() as api_client:
            #     v1 = client.CoreV1Api(api_client)
            #     await v1.list_namespace(limit=1)
            rprint("[bold green]Kubernetes configuration loaded successfully.[/bold green]")
            rprint("[yellow]Note: This checks config loading, not necessarily API reachability.[/yellow]")
        except Exception as e:
            rprint(f"[bold red]Error loading Kubernetes configuration:[/bold red] {e}")

    asyncio.run(checker()) # Keep asyncio.run here for standalone execution


# Note: Typer doesn't automatically run async functions defined with @app.command()
# unless you structure the main entry point differently (e.g., using typer.run(main_async)).
# For simplicity here, we keep asyncio.run within the command functions,
# acknowledging it creates nested loops if called from another async context,
# but ensures direct CLI execution works as intended.
# A more advanced setup might involve a single async entry point.

@app.command(name="list-anomalies")
def list_anomalies_cmd( # Keep sync def for direct Typer invocation
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum number of anomalies to list.", min=1, max=100),
):
    """
    List the most recent detected anomalies stored in the database.
    """
    setup_logging() # Configure logging

    async def fetcher():
        mongo_client, db = await _get_db()
        if not db:
            return # Error already printed

        rprint(f"\n[bold cyan]Fetching last {limit} anomalies...[/bold cyan]")
        try:
            anomalies_cursor = (
                db["anomalies"]
                .find()
                .sort("timestamp", -1)
                .limit(limit)
            )
            anomalies_raw = await anomalies_cursor.to_list(length=limit)

            if not anomalies_raw:
                rprint("[yellow]No anomalies found in the database.[/yellow]")
                return

            table = Table(title=f"Recent Anomalies (Limit: {limit})", show_header=True, header_style="bold magenta")
            table.add_column("Timestamp", style="dim", width=26)
            table.add_column("Score", justify="right")
            table.add_column("Threshold", justify="right")
            table.add_column("Source Type", justify="center")
            table.add_column("Features", max_width=50)
            table.add_column("Remediation Status")
            table.add_column("ID", style="dim")

            for raw_anomaly in anomalies_raw:
                try:
                    # Basic validation/formatting for display
                    ts = raw_anomaly.get("timestamp", "N/A")
                    if isinstance(ts, datetime.datetime):
                        ts_str = ts.isoformat(sep=" ", timespec="milliseconds")
                    else:
                        ts_str = str(ts)

                    score = raw_anomaly.get('score', float('nan'))
                    threshold = raw_anomaly.get('threshold', float('nan'))
                    features_str = str(raw_anomaly.get('features', {}))
                    # Truncate long feature strings
                    if len(features_str) > 48:
                        features_str = features_str[:45] + "..."

                    table.add_row(
                        ts_str,
                        f"{score:.4f}",
                        f"{threshold:.4f}",
                        raw_anomaly.get("source_type", "N/A"),
                        features_str,
                        raw_anomaly.get("remediation_status", "N/A"),
                        str(raw_anomaly.get("_id", "N/A")),
                    )
                except Exception as e:
                    rprint(f"[red]Error processing anomaly record {raw_anomaly.get('_id')}: {e}[/red]")

            rprint(table)

        except Exception as e:
            rprint(f"[bold red]Error fetching anomalies:[/bold red] {e}")
        finally:
            if mongo_client:
                mongo_client.close()

    asyncio.run(fetcher()) # Keep asyncio.run here

@app.command(name="validate-queries")
def validate_queries_cmd(
    retry_failed: bool = typer.Option(True, "--retry-failed", "-r", help="Retry failed queries with simpler versions"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed results for each query"),
):
    """
    Validate all Prometheus queries defined in the configuration.
    
    This command tests each query against Prometheus and reports the results.
    If retry_failed is enabled, it will attempt simpler versions of failing queries.
    """
    setup_logging()  # Configure logging

    async def validator():
        # Check Prometheus connection first
        rprint("[cyan]Checking Prometheus connection...[/cyan]")
        http_client = httpx.AsyncClient(timeout=15.0)
        try:
            test_query = "up"
            data = await prometheus.query_prometheus(http_client, test_query, prometheus_url=str(settings.prom_url))
            if not data:
                rprint("[bold red]Prometheus connection failed. Check URL: {settings.prom_url}[/bold red]")
                return
            rprint(f"[bold green]Prometheus connection successful: {settings.prom_url}[/bold green]")
            
            # Get available metrics for suggestions if queries fail
            available_metric_families = set()
            try:
                metrics_data = await prometheus.query_prometheus(http_client, "count({}) by (__name__)", prometheus_url=str(settings.prom_url))
                if metrics_data and "data" in metrics_data and "result" in metrics_data["data"]:
                    for item in metrics_data["data"]["result"]:
                        if "metric" in item and "__name__" in item["metric"]:
                            available_metric_families.add(item["metric"]["__name__"])
            except Exception as e:
                logger.warning(f"Failed to get available metrics: {e}")
            
            fallback_suggestions = {
                "container_cpu_usage_seconds_total": ["container_cpu_seconds_total", "node_cpu_seconds_total"],
                "kube_pod_container_resource_limits_cpu": ["kube_pod_container_resources", "kube_pod_spec_containers"],
                "kube_pod_container_resource_limits_memory": ["kube_pod_container_resources", "kube_pod_spec_containers"],
                "container_memory_working_set_bytes": ["container_memory_usage_bytes", "node_memory_MemTotal_bytes"],
                "kube_pod_container_status_terminated_reason": ["kube_pod_container_status_waiting", "kube_pod_status_phase"],
                "apiserver_request_duration_seconds_bucket": ["apiserver_request_total", "http_request_duration_seconds_bucket"],
            }

            # Test each query
            total_queries = len(settings.prom_queries)
            successful_queries = 0
            failed_queries = 0
            results = []

            rprint(f"[cyan]Testing {total_queries} Prometheus queries...[/cyan]")
            
            # Use a table to display results
            table = Table(title="Prometheus Query Validation Results")
            table.add_column("Query Name", style="blue")
            table.add_column("Status", style="green")
            table.add_column("Result Count", style="cyan")
            table.add_column("Execution Time", style="magenta")
            
            for query_name, query in settings.prom_queries.items():
                start_time = time.time()
                try:
                    # Clean up query whitespace
                    query = query.strip()
                    
                    result_data = await prometheus.query_prometheus(http_client, query, prometheus_url=str(settings.prom_url))
                    
                    execution_time = time.time() - start_time
                    
                    if not result_data or "data" not in result_data or "result" not in result_data["data"]:
                        # Query failed or returned empty
                        result_count = 0
                        status = "[bold red]Failed[/bold red]"
                        failed_queries += 1

                        # Try a fallback query if retry_failed is enabled
                        alternative_found = False
                        if retry_failed:
                            for metric in re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', query):
                                if metric in fallback_suggestions:
                                    for alt_metric in fallback_suggestions[metric]:
                                        if alt_metric in available_metric_families:
                                            alternative_found = True
                                            alt_status = f"[bold yellow]Try: {alt_metric}[/bold yellow]"
                                            break
                                
                                # Find similar metrics if possible
                                if not alternative_found and available_metric_families:
                                    for avail_metric in available_metric_families:
                                        if metric in avail_metric or avail_metric in metric:
                                            alt_status = f"[bold yellow]Similar: {avail_metric}[/bold yellow]"
                                            alternative_found = True
                                            break
                            
                            if not alternative_found:
                                alt_status = "[bold red]No alternative found[/bold red]"
                            
                            if verbose:
                                table.add_row(query_name, status, "0", f"{execution_time:.2f}s")
                                table.add_row("└─ Suggestion", alt_status, "", "")
                            else:
                                table.add_row(query_name, f"{status} ({alt_status})", "0", f"{execution_time:.2f}s")
                        else:
                            table.add_row(query_name, status, "0", f"{execution_time:.2f}s")
                    else:
                        # Query succeeded
                        result_count = len(result_data["data"]["result"])
                        status = "[bold green]Success[/bold green]" if result_count > 0 else "[yellow]Empty[/yellow]"
                        
                        if result_count > 0:
                            successful_queries += 1
                        else:
                            failed_queries += 1
                            
                        # Add suggestions for empty results
                        if result_count == 0 and retry_failed:
                            # Extract metric names from the query
                            has_suggestion = False
                            for metric in re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', query):
                                if metric in fallback_suggestions:
                                    for alt_metric in fallback_suggestions[metric]:
                                        if alt_metric in available_metric_families:
                                            has_suggestion = True
                                            suggestion = f"[bold yellow]Try: {alt_metric}[/bold yellow]"
                                            break
                            
                            if has_suggestion and verbose:
                                table.add_row(query_name, status, "0", f"{execution_time:.2f}s")
                                table.add_row("└─ Suggestion", suggestion, "", "")
                            else:
                                table.add_row(query_name, status, f"{result_count}", f"{execution_time:.2f}s")
                        else:
                            table.add_row(query_name, status, f"{result_count}", f"{execution_time:.2f}s")
                            
                        # Add example data for verbose output
                        if verbose and result_count > 0:
                            # Show a sample result
                            sample = result_data["data"]["result"][0]
                            sample_str = str(sample)
                            if len(sample_str) > 100:
                                sample_str = sample_str[:97] + "..."
                            table.add_row("└─ Sample", f"[dim]{sample_str}[/dim]", "", "")
                            
                except Exception as e:
                    execution_time = time.time() - start_time
                    failed_queries += 1
                    error_msg = str(e)
                    if len(error_msg) > 60:
                        error_msg = error_msg[:57] + "..."
                    
                    table.add_row(query_name, f"[bold red]Error[/bold red]", f"[red]{error_msg}[/red]", f"{execution_time:.2f}s")
            
            rprint(table)
            
            # Summary
            success_rate = (successful_queries / total_queries) * 100 if total_queries > 0 else 0
            rprint(f"\n[bold]Summary:[/bold] {successful_queries} successful queries, {failed_queries} failed queries ({success_rate:.1f}% success rate)")
            
            if failed_queries > 0:
                rprint("\n[yellow]Recommended actions for failed queries:[/yellow]")
                rprint("1. Check if your Prometheus has the required metrics exposed")
                rprint("2. Adjust your query syntax to match your specific Prometheus setup")
                rprint("3. Consider updating your exporters or adding more exporters")
                rprint("4. Run with --verbose flag to see more details about each query")
                
        except Exception as e:
            rprint(f"[bold red]Error validating queries: {e}[/bold red]")
        finally:
            await http_client.aclose()

    asyncio.run(validator())

if __name__ == "__main__":
    app()
