import json
from typing import Dict, Any, List, Optional
import asyncio

from loguru import logger
import openai
import google.generativeai as genai

from app.core.config import settings
from app.models.database import (
    IssueCreate, IssueType, IssueSeverity, K8sObjectType
)


class AIAnalyzerService:
    """Service for AI analysis of Kubernetes cluster state."""

    def __init__(self):
        """Initialize AI client."""
        self.ai_provider = settings.AI_PROVIDER
        self.initialized = False

        if self.ai_provider == "azure":
            self._init_azure_openai()
        elif self.ai_provider == "gemini":
            self._init_gemini()

    def _init_azure_openai(self):
        """Initialize Azure OpenAI client."""
        try:
            # Azure OpenAI setup for client v1.0+
            from openai import AzureOpenAI

            self.client = AzureOpenAI(
                api_key=settings.AZURE_OPENAI_API_KEY,
                api_version=settings.AZURE_OPENAI_API_VERSION,
                azure_endpoint=settings.AZURE_OPENAI_ENDPOINT
            )

            self.initialized = True
            logger.info("Azure OpenAI client initialized")
        except Exception as e:
            self.initialized = False
            logger.error(f"Failed to initialize Azure OpenAI client: {e}")

    def _init_gemini(self):
        """Initialize Google Gemini client."""
        try:
            # Google Gemini setup
            genai.configure(api_key=settings.GEMINI_API_KEY)

            # Select an appropriate model
            self.model = genai.GenerativeModel('gemini-2.0-flash')

            self.initialized = True
            logger.info("Google Gemini client initialized")
        except Exception as e:
            self.initialized = False
            logger.error(f"Failed to initialize Google Gemini client: {e}")

    async def analyze_cluster_state(self, cluster_state: Dict[str, Any]) -> List[IssueCreate]:
        """
        Analyze Kubernetes cluster state using AI.

        Args:
            cluster_state: Current state of the Kubernetes cluster

        Returns:
            List of detected issues
        """
        if not self.initialized:
            logger.error("AI analyzer not initialized")
            return []

        try:
            # Prepare data for AI analysis
            analysis_data = self._prepare_analysis_data(cluster_state)

            # Get AI analysis
            if self.ai_provider == "azure":
                analysis_result = await self._analyze_with_azure_openai(analysis_data)
            elif self.ai_provider == "gemini":
                analysis_result = await self._analyze_with_gemini(analysis_data)
            else:
                logger.error(f"Unknown AI provider: {self.ai_provider}")
                return []

            # Parse AI response into issues
            issues = self._parse_ai_response(analysis_result)

            logger.info(f"AI analysis completed, found {len(issues)} issues")
            return issues

        except Exception as e:
            logger.error(f"Error in AI analysis: {e}")
            return []

    def _prepare_analysis_data(self, cluster_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare cluster state data for AI analysis.

        Args:
            cluster_state: Raw cluster state data

        Returns:
            Simplified and structured data for AI analysis
        """
        # Extract just what we need to minimize token usage
        analysis_data = {
            "timestamp": cluster_state.get("timestamp", ""),
            "nodes": self._simplify_nodes(cluster_state.get("nodes", [])),
            "pods": self._simplify_pods(cluster_state.get("pods", [])),
            "deployments": self._simplify_deployments(cluster_state.get("deployments", [])),
            "services": self._simplify_services(cluster_state.get("services", [])),
            "events": self._simplify_events(cluster_state.get("events", [])),
            "metrics": self._simplify_metrics(cluster_state.get("metrics", {}))
        }
        return analysis_data

    def _simplify_nodes(self, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Simplify node data to include only relevant information."""
        simplified = []
        for node in nodes:
            simplified.append({
                "name": node.get("metadata", {}).get("name", ""),
                "conditions": node.get("status", {}).get("conditions", []),
                "allocatable": node.get("status", {}).get("allocatable", {}),
                "capacity": node.get("status", {}).get("capacity", {}),
                "nodeInfo": node.get("status", {}).get("nodeInfo", {})
            })
        return simplified

    def _simplify_pods(self, pods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Simplify pod data to include only relevant information."""
        simplified = []
        for pod in pods:
            simplified.append({
                "name": pod.get("metadata", {}).get("name", ""),
                "namespace": pod.get("metadata", {}).get("namespace", ""),
                "phase": pod.get("status", {}).get("phase", ""),
                "conditions": pod.get("status", {}).get("conditions", []),
                "containerStatuses": pod.get("status", {}).get("containerStatuses", []),
                "initContainerStatuses": pod.get("status", {}).get("initContainerStatuses", []),
                "hostIP": pod.get("status", {}).get("hostIP", ""),
                "podIP": pod.get("status", {}).get("podIP", ""),
                "qosClass": pod.get("status", {}).get("qosClass", ""),
                "restartPolicy": pod.get("spec", {}).get("restartPolicy", "")
            })
        return simplified

    def _simplify_deployments(self, deployments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Simplify deployment data to include only relevant information."""
        simplified = []
        for deployment in deployments:
            simplified.append({
                "name": deployment.get("metadata", {}).get("name", ""),
                "namespace": deployment.get("metadata", {}).get("namespace", ""),
                "replicas": deployment.get("spec", {}).get("replicas", 0),
                "availableReplicas": deployment.get("status", {}).get("availableReplicas", 0),
                "readyReplicas": deployment.get("status", {}).get("readyReplicas", 0),
                "updatedReplicas": deployment.get("status", {}).get("updatedReplicas", 0),
                "conditions": deployment.get("status", {}).get("conditions", [])
            })
        return simplified

    def _simplify_services(self, services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Simplify service data to include only relevant information."""
        simplified = []
        for service in services:
            simplified.append({
                "name": service.get("metadata", {}).get("name", ""),
                "namespace": service.get("metadata", {}).get("namespace", ""),
                "type": service.get("spec", {}).get("type", ""),
                "clusterIP": service.get("spec", {}).get("clusterIP", ""),
                "ports": service.get("spec", {}).get("ports", []),
                "selector": service.get("spec", {}).get("selector", {}),
                "endpoints": service.get("endpoints", {}).get("subsets", []) if "endpoints" in service else []
            })
        return simplified

    def _simplify_events(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Simplify event data to include only relevant information."""
        simplified = []
        for event in events:
            # Focus on warning events
            if event.get("type") == "Warning":
                simplified.append({
                    "type": event.get("type", ""),
                    "reason": event.get("reason", ""),
                    "message": event.get("message", ""),
                    "involvedObject": {
                        "kind": event.get("involvedObject", {}).get("kind", ""),
                        "name": event.get("involvedObject", {}).get("name", ""),
                        "namespace": event.get("involvedObject", {}).get("namespace", "")
                    },
                    "count": event.get("count", 1),
                    "lastTimestamp": event.get("lastTimestamp", "")
                })
        return simplified

    def _simplify_metrics(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Simplify metrics data to include only relevant information."""
        simplified = {"nodes": [], "pods": []}

        # Simplify node metrics
        for node in metrics.get("nodes", []):
            simplified["nodes"].append({
                "name": node.get("metadata", {}).get("name", ""),
                "usage": node.get("usage", {})
            })

        # Simplify pod metrics
        for pod in metrics.get("pods", []):
            simplified["pods"].append({
                "name": pod.get("metadata", {}).get("name", ""),
                "namespace": pod.get("metadata", {}).get("namespace", ""),
                "containers": pod.get("containers", [])
            })

        return simplified

    async def _analyze_with_azure_openai(self, analysis_data: Dict[str, Any]) -> str:
        """
        Analyze data with Azure OpenAI.

        Args:
            analysis_data: Prepared cluster state data

        Returns:
            Raw AI response text
        """
        try:
            # Convert analysis data to JSON string
            prompt = self._create_analysis_prompt(analysis_data)

            # Call Azure OpenAI API using new client version
            response = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,  # Low temperature for more deterministic results
                max_tokens=4000
            )

            # Extract response text - new client has different response structure
            return response.choices[0].message.content

        except Exception as e:
            logger.error(f"Error in Azure OpenAI analysis: {e}")
            raise

    async def _analyze_with_gemini(self, analysis_data: Dict[str, Any]) -> str:
        """
        Analyze data with Google Gemini.

        Args:
            analysis_data: Prepared cluster state data

        Returns:
            Raw AI response text
        """
        try:
            # Convert analysis data to JSON string
            prompt = self._create_analysis_prompt(analysis_data)

            # Call Gemini API
            system_prompt = self._get_system_prompt()
            full_prompt = f"{system_prompt}\n\n{prompt}"

            response = await self.model.generate_content_async(full_prompt)

            # Extract response text
            return response.text

        except Exception as e:
            logger.error(f"Error in Gemini analysis: {e}")
            raise

    def _get_system_prompt(self) -> str:
        """Get the system prompt for AI analysis."""
        return """You are an expert Kubernetes SRE specialist tasked with analyzing cluster state to detect anomalies
and predict potential future failures. Based on the provided data, identify any existing issues or potential
problems that might occur in the future.

For each identified issue:
1. Determine if it's an existing ANOMALY or a PREDICTION of a potential future failure
2. Assign severity (CRITICAL, WARNING, INFO) based on potential impact
3. Identify the affected Kubernetes object type (Node, Pod, Deployment, Service, etc.)
4. Provide a clear description of the issue
5. Suggest steps for remediation
6. Provide a safe kubectl command that would help diagnose or fix the issue

Format your response as a JSON array of issues with the following structure:
[
  {
    "type": "ANOMALY", // or "PREDICTION"
    "severity": "CRITICAL", // or "WARNING" or "INFO"
    "k8s_object_type": "Pod", // or "Node", "Deployment", "Service", "PersistentVolumeClaim", "StatefulSet", "DaemonSet", "Ingress"
    "k8s_object_name": "example-pod-name",
    "k8s_object_namespace": "default", // omit for cluster-scoped resources like Nodes
    "description": "Detailed description of the issue",
    "remediation_suggestion": "Steps to fix the issue",
    "remediation_command": "kubectl command to help diagnose or fix",
    "ai_confidence_score": 0.95 // a number between 0 and 1 indicating confidence
  }
]

IMPORTANT SAFETY GUIDELINES:
- For remediation commands, prioritize read-only diagnostic commands (get, describe, logs)
- Only suggest potentially destructive commands (delete, scale, etc.) if:
  1. They are clearly appropriate for the situation
  2. The issue is severe enough to warrant such action
  3. You are highly confident (>0.8) that the command will help
- Always use the full resource name, namespace, and other identifiers in kubectl commands
- Never suggest commands that could damage the entire cluster (e.g., deleting namespaces, critical resources)
- For dangerous commands, include a warning in the remediation suggestion
"""

    def _create_analysis_prompt(self, analysis_data: Dict[str, Any]) -> str:
        """Create the prompt for AI analysis."""
        # Convert analysis data to JSON string
        data_json = json.dumps(analysis_data, indent=2)

        prompt = f"""Please analyze this Kubernetes cluster state data:

```json
{data_json}
```

Identify any anomalies and predict potential future failures based on the data.
Return the results in the specified JSON format."""

        return prompt

    def _parse_ai_response(self, response_text: str) -> List[IssueCreate]:
        """
        Parse AI response text into a list of issues.

        Args:
            response_text: Raw AI response text

        Returns:
            List of issue models
        """
        issues = []

        try:
            # Extract JSON from response (in case there's text before/after)
            json_start = response_text.find('[')
            json_end = response_text.rfind(']') + 1

            if json_start >= 0 and json_end > json_start:
                json_text = response_text[json_start:json_end]
                response_data = json.loads(json_text)

                # Convert each item to IssueCreate model
                for item in response_data:
                    try:
                        # Handle issue type and severity
                        issue_type_str = item.get("type", "").upper()
                        severity_str = item.get("severity", "").upper()

                        # If type is a severity level, swap them
                        if issue_type_str in ["CRITICAL", "WARNING", "INFO"]:
                            logger.warning(f"Using '{issue_type_str}' as severity instead of issue type")
                            severity_str = issue_type_str
                            issue_type_str = "ANOMALY"  # Default to ANOMALY for invalid types

                        # Validate and set issue type
                        try:
                            issue_type = IssueType(issue_type_str)
                        except ValueError:
                            logger.warning(f"Invalid issue type '{issue_type_str}', defaulting to ANOMALY")
                            issue_type = IssueType.ANOMALY

                        # Validate and set severity
                        try:
                            severity = IssueSeverity(severity_str)
                        except ValueError:
                            logger.warning(f"Invalid severity '{severity_str}', defaulting to INFO")
                            severity = IssueSeverity.INFO

                        # Validate k8s object type
                        try:
                            k8s_object_type = K8sObjectType(item.get("k8s_object_type", "Other"))
                        except ValueError:
                            # If invalid, default to "Other"
                            k8s_object_type = K8sObjectType.OTHER

                        # Create issue
                        issue = IssueCreate(
                            type=issue_type,
                            severity=severity,
                            k8s_object_type=k8s_object_type,
                            k8s_object_name=item.get("k8s_object_name", ""),
                            k8s_object_namespace=item.get("k8s_object_namespace"),
                            description=item.get("description", ""),
                            remediation_suggestion=item.get("remediation_suggestion", ""),
                            remediation_command=item.get("remediation_command"),
                            ai_confidence_score=item.get("ai_confidence_score")
                        )

                        issues.append(issue)
                    except (ValueError, KeyError) as e:
                        logger.warning(f"Error parsing issue from AI response: {e}")
                        continue
            else:
                logger.warning("No valid JSON found in AI response")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response as JSON: {e}")
            logger.debug(f"Response text: {response_text}")

        return issues


# Create a global instance for use across the application
ai_analyzer = AIAnalyzerService()
