from flask import Flask, jsonify, request
from flask_cors import CORS
import os
from dotenv import load_dotenv
import json
import datetime

# Import custom modules
from prometheus_client import get_cluster_metrics, get_prometheus_status
from agent.agent import run_agent, set_auto_remediation
from k8s_client import get_cluster_info, perform_remediation_action

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Configuration
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "https://localhost:9090")
PORT = int(os.environ.get("PORT", 8000))  # Using 8000 instead of 5000

# In-memory storage for remediation history
# In production, this could be a database
remediation_history = []

# Settings
settings = {
    "autoRemediation": True,
    "pollInterval": 30
}

# API Routes
@app.route('/api/status', methods=['GET'])
def get_status():
    """Get the overall status of the system and cluster"""
    prometheus_status = get_prometheus_status(PROMETHEUS_URL)

    # Get basic cluster info
    cluster_info = get_cluster_info()

    # Get metrics to determine health
    metrics = get_cluster_metrics(PROMETHEUS_URL)

    # Determine cluster health based on metrics
    cluster_health = "Healthy"
    if metrics:
        cpu_percent = metrics.get("cpuPercent", 0)
        memory_percent = metrics.get("memoryPercent", 0)
        failed_pods = metrics.get("failedPods", 0)

        if cpu_percent > 85 or memory_percent > 85 or failed_pods > 0:
            cluster_health = "Degraded"
        if cpu_percent > 95 or memory_percent > 95 or failed_pods > 3:
            cluster_health = "Critical"

    # Combine all status information
    status = {
        "prometheus": "up" if prometheus_status else "down",
        "clusterHealth": cluster_health,
        "agentAutoRemediation": settings["autoRemediation"],
        "nodes": cluster_info.get("nodes", 0),
        "pods": cluster_info.get("pods", 0)
    }

    # Add metric percentages if available
    if metrics:
        status["cpuUsagePercent"] = metrics.get("cpuPercent", 0)
        status["memoryUsagePercent"] = metrics.get("memoryPercent", 0)

    return jsonify(status)

@app.route('/api/metrics', methods=['GET'])
def get_metrics():
    """Get detailed metrics for visualization"""
    metrics = get_cluster_metrics(PROMETHEUS_URL)
    if not metrics:
        return jsonify({"error": "Failed to fetch metrics from Prometheus"}), 500

    return jsonify(metrics)

@app.route('/api/remediations', methods=['GET'])
def get_remediations():
    """Get history of remediation actions"""
    # Could implement pagination here if history gets large
    return jsonify(remediation_history)

@app.route('/api/remediations', methods=['POST'])
def trigger_remediation():
    """Manually trigger a remediation action"""
    action_data = request.json

    if not action_data:
        return jsonify({"error": "No action specified"}), 400

    # Validate action type
    action_type = action_data.get("type")
    target = action_data.get("target")

    if not action_type or not target:
        return jsonify({"error": "Action type and target are required"}), 400

    # Perform the action
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        result = perform_remediation_action(action_type, target, action_data.get("parameters", {}))

        # Record the action in history
        remediation_record = {
            "time": timestamp,
            "action": result.get("description", f"{action_type} on {target}"),
            "reason": "Manual action",
            "initiatedBy": "manual",
            "status": "success"
        }
        remediation_history.insert(0, remediation_record)  # Add to front of list

        return jsonify(remediation_record)

    except Exception as e:
        # Record failed action
        error_record = {
            "time": timestamp,
            "action": f"{action_type} on {target}",
            "initiatedBy": "manual",
            "status": "failed",
            "error": str(e)
        }
        remediation_history.insert(0, error_record)
        return jsonify(error_record), 500

@app.route('/api/settings', methods=['POST'])
def update_settings():
    """Update system settings"""
    new_settings = request.json

    if not new_settings:
        return jsonify({"error": "No settings provided"}), 400

    # Update settings
    if "autoRemediation" in new_settings:
        settings["autoRemediation"] = bool(new_settings["autoRemediation"])
        # Update agent auto-remediation flag
        set_auto_remediation(settings["autoRemediation"])

    if "pollInterval" in new_settings:
        # Validate polling interval (minimum 5 seconds)
        new_interval = int(new_settings["pollInterval"])
        if new_interval < 5:
            return jsonify({"error": "Poll interval must be at least 5 seconds"}), 400
        settings["pollInterval"] = new_interval

    return jsonify(settings)

@app.route('/api/agent/analyze', methods=['GET'])
def analyze_cluster():
    """Trigger a one-time analysis by the agent without taking action"""
    metrics = get_cluster_metrics(PROMETHEUS_URL)
    if not metrics:
        return jsonify({"error": "Failed to fetch metrics from Prometheus"}), 500

    # Run agent with auto-execute=False to only get recommendations
    recommendations = run_agent(metrics, execute_actions=False)

    return jsonify({"recommendations": recommendations})

def register_remediation(action, reason, status="success", error=None, initiated_by="auto"):
    """Register a remediation action in history"""
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    record = {
        "time": timestamp,
        "action": action,
        "reason": reason,
        "initiatedBy": initiated_by,
        "status": status
    }

    if error:
        record["error"] = str(error)

    remediation_history.insert(0, record)  # Add to front of list
    return record

# Initialize agent background task (in a real app, this would be a separate thread/process)
# But we're simplifying for this implementation
def init_agent_task():
    """Initialize the agent background task"""
    # In a real application, this would start a background thread
    # For simplicity, we'll just set up the agent here
    print("Agent task initialized")

if __name__ == '__main__':
    # Initialize agent task
    init_agent_task()

    # Start the Flask app
    app.run(host='0.0.0.0', port=PORT, debug=True)
