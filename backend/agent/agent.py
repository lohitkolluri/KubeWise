from langchain.agents import Tool, AgentExecutor
from langchain_core.prompts import PromptTemplate
from langchain_core.language_models.llms import BaseLLM
# from langchain_google_vertexai import VertexAI
from langgraph.graph import StateGraph
import os
import logging
import time
import json
from typing import Dict, List, Optional, Any, Callable
import google.generativeai as genai

# Import Kubernetes client for remediation actions
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from k8s_client import (
    restart_pod,
    scale_deployment,
    cordon_node,
    drain_node,
    update_deployment_resources,
    get_pod_logs
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

# Initialize Google Generative AI with API key
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    logger.warning("GOOGLE_API_KEY not set. Direct Gemini integration will not work.")

# Global settings
AUTO_REMEDIATION = True

def set_auto_remediation(enabled):
    """Set whether the agent should auto-remediate issues"""
    global AUTO_REMEDIATION
    AUTO_REMEDIATION = enabled
    logger.info(f"Auto-remediation set to: {enabled}")

# Tool definitions
class MetricsTool:
    """Tool to get cluster metrics"""

    def __init__(self, metrics=None):
        self.metrics = metrics or {}

    def __call__(self, query=None):
        """Get metrics based on optional query parameter"""
        if not query or query.lower() == "all":
            return json.dumps(self.metrics, indent=2)

        # Check if specific metric was requested
        parts = query.lower().split()
        for part in parts:
            if part in self.metrics:
                return f"{part}: {self.metrics[part]}"

        # Return CPU and memory as default if query doesn't match
        if "cpu" in query.lower():
            return f"CPU: {self.metrics.get('cpuPercent', 'N/A')}%"
        elif "memory" in query.lower() or "ram" in query.lower():
            return f"Memory: {self.metrics.get('memoryPercent', 'N/A')}%"
        elif "pod" in query.lower():
            return f"Pods: {self.metrics.get('runningPods', 0)} running, {self.metrics.get('pendingPods', 0)} pending, {self.metrics.get('failedPods', 0)} failed"

        # Default response if no match
        return json.dumps(self.metrics, indent=2)

class LogsTool:
    """Tool to get pod logs"""

    def __call__(self, pod_name, namespace="default", container=None, tail_lines=100):
        """Get logs from a specific pod"""
        try:
            logs = get_pod_logs(pod_name, namespace, container, tail_lines)
            # Truncate logs if too long
            if len(logs) > 1500:
                logs = logs[:1500] + "...[truncated]"
            return logs
        except Exception as e:
            return f"Error fetching logs: {str(e)}"

class RestartPodTool:
    """Tool to restart a pod"""

    def __call__(self, pod_name, namespace="default"):
        """Restart a pod by deleting it"""
        if not AUTO_REMEDIATION:
            return f"Would restart pod {pod_name} in namespace {namespace} (auto-remediation disabled)"

        try:
            result = restart_pod(pod_name, namespace)
            return f"Successfully restarted pod {pod_name} in namespace {namespace}"
        except Exception as e:
            return f"Failed to restart pod {pod_name}: {str(e)}"

class ScaleDeploymentTool:
    """Tool to scale a deployment"""

    def __call__(self, deployment_name, replicas, namespace="default"):
        """Scale a deployment to a specific number of replicas"""
        if not AUTO_REMEDIATION:
            return f"Would scale deployment {deployment_name} to {replicas} replicas (auto-remediation disabled)"

        try:
            result = scale_deployment(deployment_name, namespace, int(replicas))
            return f"Successfully scaled deployment {deployment_name} to {replicas} replicas"
        except Exception as e:
            return f"Failed to scale deployment {deployment_name}: {str(e)}"

class UpdateResourcesTool:
    """Tool to update deployment resource limits/requests"""

    def __call__(self, deployment_name, namespace="default", cpu_limit=None, memory_limit=None, cpu_request=None, memory_request=None):
        """Update resource limits and requests for a deployment"""
        if not AUTO_REMEDIATION:
            return f"Would update resources for deployment {deployment_name} (auto-remediation disabled)"

        try:
            result = update_deployment_resources(
                deployment_name,
                namespace,
                cpu_limit,
                memory_limit,
                cpu_request,
                memory_request
            )
            return f"Successfully updated resources for deployment {deployment_name}"
        except Exception as e:
            return f"Failed to update resources for deployment {deployment_name}: {str(e)}"

class GeminiLLM(BaseLLM):
    """LLM implementation that uses google.generativeai directly"""

    def __init__(self, model_name="gemini-1.5-pro", temperature=0, max_output_tokens=1024):
        """Initialize with Gemini model parameters"""
        super().__init__()
        self.model_name = model_name  # Changed from _model_name to model_name
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.model = None

        # Configure the model
        generation_config = {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }

        try:
            # Initialize the model
            self.model = genai.GenerativeModel(
                model_name=self.model_name,  # Changed from _model_name to model_name
                generation_config=generation_config
            )
            logger.info(f"Initialized Gemini model {self.model_name}")  # Changed from _model_name to model_name
        except Exception as e:
            logger.error(f"Failed to initialize Gemini model: {str(e)}")
            self.model = None

    def _call(self, prompt: str, stop=None, **kwargs):
        """Call the Gemini model with the prompt"""
        if not self.model:
            return "Error: Gemini model not initialized. Check if GOOGLE_API_KEY is set."

        try:
            response = self.model.generate_content(prompt)
            return response.text
        except Exception as e:
            logger.error(f"Error calling Gemini API: {str(e)}")
            return f"Error calling Gemini API: {str(e)}"

    @property
    def _llm_type(self) -> str:
        """Return type of LLM"""
        return "gemini"

    def _generate(self, prompts, stop=None, **kwargs):
        """Implement required _generate method"""
        from langchain_core.outputs import Generation, LLMResult
        generations = [[Generation(text=self._call(prompt, stop=stop, **kwargs))] for prompt in prompts]
        return LLMResult(generations=generations)

def initialize_agent(metrics):
    """Initialize the agent with tools and LLM"""

    # Create tools with current metrics
    metrics_tool = MetricsTool(metrics)
    logs_tool = LogsTool()
    restart_pod_tool = RestartPodTool()
    scale_deployment_tool = ScaleDeploymentTool()
    update_resources_tool = UpdateResourcesTool()

    tools = [
        Tool(
            name="GetMetrics",
            func=metrics_tool,
            description="Get current cluster metrics. You can ask for 'all' or specific metrics like 'cpu', 'memory', or 'pods'."
        ),
        Tool(
            name="GetLogs",
            func=logs_tool,
            description="Get logs from a pod. Parameters: pod_name, namespace='default', container=None, tail_lines=100"
        ),
        Tool(
            name="RestartPod",
            func=restart_pod_tool,
            description="Restart a pod by deleting it (it will be recreated by its controller). Parameters: pod_name, namespace='default'"
        ),
        Tool(
            name="ScaleDeployment",
            func=scale_deployment_tool,
            description="Scale a deployment to a specific number of replicas. Parameters: deployment_name, replicas, namespace='default'"
        ),
        Tool(
            name="UpdateResources",
            func=update_resources_tool,
            description="Update resource limits and requests for a deployment. Parameters: deployment_name, namespace='default', cpu_limit=None, memory_limit=None, cpu_request=None, memory_request=None"
        )
    ]

    # Initialize LLM
    if GOOGLE_API_KEY:
        llm = GeminiLLM()
        logger.info("Using Gemini via google.generativeai")
    else:
        # No fallback, just raise an exception
        raise ValueError("No GOOGLE_API_KEY provided. Cannot initialize Gemini LLM.")

    # Create the system and user messages for the agent
    system_prompt = """You are a Kubernetes SRE assistant with expertise in cluster monitoring and remediation.
Your goal is to analyze cluster metrics and identify potential issues that need remediation.
You have access to several tools to help you investigate and fix issues:
- GetMetrics: To check cluster metrics like CPU, memory usage, and pod status
- GetLogs: To check the logs of specific pods that might be having issues
- RestartPod: To restart pods that are stuck or in crash loops
- ScaleDeployment: To scale deployments up or down based on resource usage
- UpdateResources: To update resource limits and requests for deployments

Follow these guidelines:
1. Check the metrics to identify potential issues (high CPU/memory, failed pods, etc.)
2. If you detect an issue, investigate further using logs or metrics
3. Recommend or execute remediation actions as appropriate
4. Explain your reasoning and actions clearly
5. If auto-remediation is disabled, suggest actions but don't execute them

Examples of issues to look for:
- High CPU usage (>80%): Consider scaling the affected deployment
- Memory pressure (>80%): Check for memory leaks or increase limits
- Pods in CrashLoopBackOff: Check logs and restart the pod
- Many pending pods: Check if nodes have enough resources
- Failed pods: Investigate logs and restart if appropriate

Only take action if there's a clear issue that needs remediation.
If everything looks healthy, simply state that no issues were detected.
"""

    # Define the agent using ReAct approach
    from langchain.agents import create_react_agent
    from langchain_core.prompts import PromptTemplate

    # Create a proper prompt template from the system prompt
    react_prompt_template = PromptTemplate.from_template(
        """
        {system_message}

        QUESTION: {input}

        Available tools:
        {tools}

        Tool Names: {tool_names}

        {agent_scratchpad}
        """
    )

    # Add system message to template
    prompt_with_system = react_prompt_template.partial(system_message=system_prompt)

    agent = create_react_agent(llm, tools, prompt_with_system)

    # Create the agent executor
    agent_executor = AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        max_iterations=10,
        max_execution_time=60,
        return_intermediate_steps=True,
        handle_parsing_errors=True
    )

    return agent_executor

def run_agent(metrics, execute_actions=None):
    """
    Run the agent with the current metrics

    Args:
        metrics: Dictionary of current cluster metrics
        execute_actions: Whether to execute actions (if None, use global AUTO_REMEDIATION)

    Returns:
        Agent's recommendations or actions
    """
    # Use global AUTO_REMEDIATION if execute_actions not specified
    if execute_actions is None:
        execute_actions = AUTO_REMEDIATION

    # Override global AUTO_REMEDIATION temporarily if needed
    original_auto_remediation = AUTO_REMEDIATION
    if execute_actions != original_auto_remediation:
        set_auto_remediation(execute_actions)

    try:
        # For now, since we're having issues with the LLM integration,
        # provide a direct cluster analysis based on the metrics

        # Check for issues in the metrics
        issues = []
        if metrics.get('cpuPercent', 0) > 80:
            issues.append("High CPU usage detected.")
        if metrics.get('memoryPercent', 0) > 80:
            issues.append("High memory usage detected.")
        if metrics.get('crashLoopPods', 0) > 0:
            issues.append(f"{metrics.get('crashLoopPods')} pods are in CrashLoopBackOff state.")
        if metrics.get('failedPods', 0) > 0:
            issues.append(f"{metrics.get('failedPods')} pods are in Failed state.")
        if metrics.get('pendingPods', 0) > 0:
            issues.append(f"{metrics.get('pendingPods')} pods are in Pending state.")

        # Generate a recommendation based on findings
        if issues:
            recommendation = "The following issues were detected in the cluster:\n\n"
            recommendation += "\n".join([f"- {issue}" for issue in issues])
            recommendation += "\n\nRemediation suggestions:\n"

            if metrics.get('cpuPercent', 0) > 80:
                recommendation += "\n- Consider scaling up deployments with high CPU usage."
            if metrics.get('memoryPercent', 0) > 80:
                recommendation += "\n- Check for memory leaks or increase memory limits on deployments."
            if metrics.get('crashLoopPods', 0) > 0 or metrics.get('failedPods', 0) > 0:
                recommendation += "\n- Check pod logs to identify and fix underlying issues, then restart affected pods."
            if metrics.get('pendingPods', 0) > 0:
                recommendation += "\n- Ensure nodes have sufficient resources to schedule pending pods."
        else:
            recommendation = "The cluster appears to be in a healthy state based on the current metrics:\n\n"
            recommendation += f"- CPU Usage: {metrics.get('cpuPercent', 0):.1f}%\n"
            recommendation += f"- Memory Usage: {metrics.get('memoryPercent', 0):.1f}%\n"
            recommendation += f"- Nodes: {metrics.get('nodeCount', 0)}\n"
            recommendation += f"- Running Pods: {metrics.get('runningPods', 0)}\n"
            recommendation += "\nNo remediation actions are necessary at this time."

        return {
            "recommendation": recommendation,
            "actions": []
        }

    except Exception as e:
        logger.error(f"Error running agent: {str(e)}")

        return {
            "recommendation": f"Error analyzing cluster: {str(e)}",
            "actions": []
        }
    finally:
        # Reset AUTO_REMEDIATION if it was temporarily changed
        if execute_actions != original_auto_remediation:
            set_auto_remediation(original_auto_remediation)
