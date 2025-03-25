import requests
import json
import time
from prometheus_api_client import PrometheusConnect
from requests.exceptions import RequestException

def get_prometheus_status(prometheus_url):
    """Check if Prometheus is reachable and running"""
    try:
        response = requests.get(f"{prometheus_url}/-/healthy", timeout=5, verify=False)
        return response.status_code == 200
    except RequestException:
        return False

def get_cluster_metrics(prometheus_url):
    """
    Fetch and process metrics from Prometheus
    Returns a dictionary of metrics with current values
    """
    try:
        # Create Prometheus client
        prometheus = PrometheusConnect(url=prometheus_url, disable_ssl=True)

        # Current timestamp for queries
        current_time = time.time()

        # Define queries to run
        queries = {
            # CPU usage percentage across cluster
            "cpu_usage": "100 * (1 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) by (instance))",

            # Memory usage percentage
            "memory_usage": "100 * (1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes))",

            # Node count (ready nodes)
            "node_count": "count(kube_node_status_condition{condition='Ready',status='true'})",

            # Pod count (running)
            "pod_count_running": "count(kube_pod_status_phase{phase='Running'})",

            # Pod count (failed)
            "pod_count_failed": "count(kube_pod_status_phase{phase='Failed'})",

            # Pod count (pending)
            "pod_count_pending": "count(kube_pod_status_phase{phase='Pending'})",

            # Pods in crash loop
            "pods_crash_loop": "count(kube_pod_container_status_waiting_reason{reason='CrashLoopBackOff'})",

            # Disk usage percentage
            "disk_usage": "100 * (1 - sum(node_filesystem_avail_bytes) / sum(node_filesystem_size_bytes))",

            # Network I/O
            "network_receive": "sum(rate(node_network_receive_bytes_total[5m]))",
            "network_transmit": "sum(rate(node_network_transmit_bytes_total[5m]))"
        }

        # Execute queries
        results = {}
        for metric_name, query in queries.items():
            try:
                result = prometheus.custom_query(query)
                # Extract scalar results
                if result and isinstance(result, list) and len(result) > 0:
                    # Handle vector results (multiple time series)
                    if 'value' in result[0]:
                        # Standard Prometheus response format
                        value = float(result[0]['value'][1])
                        results[metric_name] = value
                    elif isinstance(result[0], dict) and 'value' in result[0]:
                        # Another possible format
                        value = float(result[0]['value'][1])
                        results[metric_name] = value
                    else:
                        # Try to get the value field from the result
                        for item in result:
                            if 'value' in item:
                                value = float(item['value'][1])
                                results[metric_name] = value
                                break
            except Exception as e:
                print(f"Error fetching {metric_name}: {str(e)}")
                # Continue with other metrics if one fails
                continue

        # Process results into a more frontend-friendly format
        metrics = {
            "cpuPercent": results.get("cpu_usage", 0),
            "memoryPercent": results.get("memory_usage", 0),
            "diskPercent": results.get("disk_usage", 0),
            "nodeCount": int(results.get("node_count", 0)),
            "runningPods": int(results.get("pod_count_running", 0)),
            "pendingPods": int(results.get("pod_count_pending", 0)),
            "failedPods": int(results.get("pod_count_failed", 0)),
            "crashLoopPods": int(results.get("pods_crash_loop", 0)),
            "networkReceive": results.get("network_receive", 0),
            "networkTransmit": results.get("network_transmit", 0),
        }

        # Add some combined metrics
        metrics["totalPods"] = metrics["runningPods"] + metrics["pendingPods"] + metrics["failedPods"]
        metrics["networkTotal"] = metrics["networkReceive"] + metrics["networkTransmit"]

        # Optional: Get time-series data for charts (last hour)
        # This would be more complex and require range queries
        # We'll leave it simplified for now

        return metrics

    except Exception as e:
        print(f"Error fetching metrics from Prometheus: {str(e)}")
        return None

def get_pod_metrics(prometheus_url, namespace=None):
    """
    Get metrics for individual pods
    Optionally filter by namespace
    """
    try:
        prometheus = PrometheusConnect(url=prometheus_url, disable_ssl=True)

        # Base query for pod CPU usage
        cpu_query = "sum(rate(container_cpu_usage_seconds_total{container!='POD',container!=''}[5m])) by (pod, namespace)"

        # Base query for pod memory usage
        memory_query = "sum(container_memory_working_set_bytes{container!='POD',container!=''}) by (pod, namespace)"

        # Apply namespace filter if provided
        if namespace:
            cpu_query = f"sum(rate(container_cpu_usage_seconds_total{{container!='POD',container!='',namespace='{namespace}'}}[5m])) by (pod)"
            memory_query = f"sum(container_memory_working_set_bytes{{container!='POD',container!='',namespace='{namespace}'}}) by (pod)"

        # Execute queries
        cpu_results = prometheus.custom_query(cpu_query)
        memory_results = prometheus.custom_query(memory_query)

        # Process results
        pod_metrics = []

        # Process CPU metrics
        for result in cpu_results:
            pod_name = result['metric'].get('pod', 'unknown')
            namespace_name = result['metric'].get('namespace', 'unknown')
            cpu_value = float(result['value'][1])

            # Find or create entry for this pod
            pod_entry = next((p for p in pod_metrics if p['podName'] == pod_name and p['namespace'] == namespace_name), None)

            if pod_entry:
                pod_entry['cpuUsage'] = cpu_value
            else:
                pod_metrics.append({
                    'podName': pod_name,
                    'namespace': namespace_name,
                    'cpuUsage': cpu_value,
                    'memoryUsage': 0
                })

        # Process memory metrics
        for result in memory_results:
            pod_name = result['metric'].get('pod', 'unknown')
            namespace_name = result['metric'].get('namespace', 'unknown')
            memory_value = float(result['value'][1]) / (1024 * 1024)  # Convert to MB

            # Find or create entry for this pod
            pod_entry = next((p for p in pod_metrics if p['podName'] == pod_name and p['namespace'] == namespace_name), None)

            if pod_entry:
                pod_entry['memoryUsage'] = memory_value
            else:
                pod_metrics.append({
                    'podName': pod_name,
                    'namespace': namespace_name,
                    'cpuUsage': 0,
                    'memoryUsage': memory_value
                })

        return pod_metrics

    except Exception as e:
        print(f"Error fetching pod metrics: {str(e)}")
        return []

def get_metric_history(prometheus_url, metric_name, duration="1h", step="1m"):
    """
    Get historical data for a specific metric

    Args:
        prometheus_url: Prometheus server URL
        metric_name: Name of the metric (matches one of the keys in get_cluster_metrics)
        duration: Look back duration (e.g., "1h" for 1 hour)
        step: Resolution step (e.g., "1m" for 1 minute intervals)

    Returns:
        List of [timestamp, value] pairs
    """
    try:
        prometheus = PrometheusConnect(url=prometheus_url, disable_ssl=True)

        # Map metric name to PromQL query
        metric_queries = {
            "cpuPercent": "100 * (1 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) by (instance))",
            "memoryPercent": "100 * (1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes))",
            "diskPercent": "100 * (1 - sum(node_filesystem_avail_bytes) / sum(node_filesystem_size_bytes))",
            "runningPods": "count(kube_pod_status_phase{phase='Running'})",
            "networkTotal": "sum(rate(node_network_receive_bytes_total[5m])) + sum(rate(node_network_transmit_bytes_total[5m]))"
        }

        if metric_name not in metric_queries:
            return []

        # Current time
        end_time = time.time()

        # Calculate start time based on duration string
        if duration.endswith("h"):
            hours = int(duration[:-1])
            start_time = end_time - (hours * 3600)
        elif duration.endswith("m"):
            minutes = int(duration[:-1])
            start_time = end_time - (minutes * 60)
        elif duration.endswith("d"):
            days = int(duration[:-1])
            start_time = end_time - (days * 86400)
        else:
            # Default to 1 hour
            start_time = end_time - 3600

        # Calculate step in seconds
        if step.endswith("m"):
            step_seconds = int(step[:-1]) * 60
        elif step.endswith("s"):
            step_seconds = int(step[:-1])
        else:
            # Default to 60 seconds
            step_seconds = 60

        # Get the query
        query = metric_queries[metric_name]

        # Execute range query
        result = prometheus.custom_query_range(
            query=query,
            start_time=start_time,
            end_time=end_time,
            step=step_seconds
        )

        # Process results
        if result and len(result) > 0:
            # Extract the values array
            values = result[0]['values']  # [[timestamp, value], ...]

            # Convert to frontend format
            time_series = [[ts, float(val)] for ts, val in values]
            return time_series

        return []

    except Exception as e:
        print(f"Error fetching metric history: {str(e)}")
        return []
