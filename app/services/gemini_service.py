from typing import Dict, Any, List, Optional
import json
from loguru import logger
import google.generativeai as genai
from app.core.config import settings
from pydantic import BaseModel, Field
from datetime import datetime
import re

# --- Pydantic Models for Function Calling ---

class AnomalyAnalysis(BaseModel):
    """Model for anomaly analysis output."""
    root_cause: str = Field(..., description="Probable root cause of the anomaly")
    impact: str = Field(..., description="Potential impact on the system or application")
    severity: str = Field(..., description="Estimated severity (e.g., High, Medium, Low)")
    confidence: float = Field(..., description="Confidence score (0.0-1.0) in the analysis")
    recommendations: List[str] = Field(..., description="Specific recommendations for investigation")

class RemediationSteps(BaseModel):
    """Model for remediation steps output."""
    steps: List[str] = Field(..., description="Suggested remediation command strings (e.g., 'operation_name param1=value param2=value')")
    risks: List[str] = Field(..., description="Potential risks associated with the steps")
    expected_outcome: str = Field(..., description="What should happen if the steps are successful")
    confidence: float = Field(..., description="Confidence score (0.0-1.0) in the suggested remediation")

class MetricInsights(BaseModel):
    """Model for metric insights output."""
    patterns: List[str] = Field(..., description="Patterns observed in the metrics")
    anomalies: List[str] = Field(..., description="Potential anomalies detected")
    health_assessment: str = Field(..., description="Overall health assessment")
    recommendations: List[str] = Field(..., description="Recommendations for improvement")

# NEW Models for Query Generation and Verification
class GeneratedPromqlQueries(BaseModel):
    queries: List[str] = Field(..., description="List of generated PromQL query strings suitable for monitoring common Kubernetes issues.")
    reasoning: str = Field(..., description="Explanation for why these queries were chosen based on the context.")

class VerificationResult(BaseModel):
    success: bool = Field(..., description="True if the remediation is assessed as successful, False otherwise.")
    reasoning: str = Field(..., description="Explanation for the success/failure assessment based on the provided context.")
    confidence: float = Field(..., description="Confidence score (0.0-1.0) in the verification assessment.")

# NEW Models for Smart Remediation
class RemediationAction(BaseModel):
    """Structured template-based remediation action"""
    action_type: str = Field(..., description="Type of remediation action (e.g., restart_deployment, scale_deployment)")
    resource_type: str = Field(..., description="Resource type to act on (e.g., deployment, pod, node)")
    resource_name: str = Field(..., description="Name of the resource to act on")
    namespace: Optional[str] = Field(None, description="Kubernetes namespace of the resource")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Additional parameters for the action")

class PrioritizedRemediation(BaseModel):
    """Model for prioritized remediation actions"""
    actions: List[RemediationAction] = Field(..., description="Prioritized list of remediation actions")
    reasoning: str = Field(..., description="Explanation for the prioritization")
    estimated_impact: Dict[str, str] = Field(..., description="Estimated impact of each action (e.g., performance, availability)")
    phased_approach: bool = Field(..., description="Whether a phased/incremental approach is recommended")
    phased_steps: Optional[List[str]] = Field(None, description="If phased, the recommended steps in sequence")

class ResourceOptimizationSuggestion(BaseModel):
    """Model for resource optimization suggestions"""
    resource_type: str = Field(..., description="Type of resource (e.g., deployment, statefulset)")
    resource_name: str = Field(..., description="Name of the resource")
    namespace: str = Field(..., description="Namespace of the resource")
    current_cpu_request: Optional[str] = Field(None, description="Current CPU request")
    current_cpu_limit: Optional[str] = Field(None, description="Current CPU limit")
    current_memory_request: Optional[str] = Field(None, description="Current memory request")
    current_memory_limit: Optional[str] = Field(None, description="Current memory limit")
    suggested_cpu_request: Optional[str] = Field(None, description="Suggested CPU request")
    suggested_cpu_limit: Optional[str] = Field(None, description="Suggested CPU limit")
    suggested_memory_request: Optional[str] = Field(None, description="Suggested memory request")
    suggested_memory_limit: Optional[str] = Field(None, description="Suggested memory limit")
    reasoning: str = Field(..., description="Reasoning behind the suggestion")
    confidence: float = Field(..., description="Confidence in the suggestion (0.0-1.0)")

class RootCauseAnalysisReport(BaseModel):
    """Model for detailed RCA reports"""
    title: str = Field(..., description="Title summarizing the issue")
    summary: str = Field(..., description="Executive summary of the incident")
    root_cause: str = Field(..., description="Identified root cause")
    detection_details: Dict[str, Any] = Field(..., description="Details of how the issue was detected")
    timeline: List[Dict[str, str]] = Field(..., description="Timeline of the incident")
    resolution_steps: List[str] = Field(..., description="Steps taken to resolve the issue")
    metrics_analysis: str = Field(..., description="Analysis of relevant metrics")
    recommendations: List[str] = Field(..., description="Recommendations to prevent recurrence")
    related_resources: List[str] = Field(..., description="Related Kubernetes resources")

class GeminiService:
    """
    Service for interacting with Google's Gemini AI for metric analysis.
    """
    def __init__(self):
        """Initialize the Gemini service with API key."""
        self.model = None
        self._is_initialized = False
        if settings.GEMINI_API_KEY:
            try:
                genai.configure(api_key=settings.GEMINI_API_KEY)
                self.model = genai.GenerativeModel('gemini-1.5-pro')
                self._is_initialized = True
                logger.info("Gemini service initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini service: {e}")
        else:
            logger.warning("Gemini service disabled: GEMINI_API_KEY not set.")

    async def _call_gemini_with_function(self, prompt: str, output_model: BaseModel) -> Optional[Dict[str, Any]]:
        """Helper to call Gemini API with function calling using a compatible approach."""
        if not self.model:
            logger.warning("Gemini service not configured or unavailable.")
            return {"error": "Gemini service not configured or unavailable."}

        try:
            model_name = output_model.__name__

            # Create standard Gemini function declaration (not OpenAI-compatible)
            function_declaration = {
                "name": f"extract_{model_name}",
                "description": f"Extract information according to the {model_name} schema.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        field: {
                            "type": self._pydantic_to_gemini_type(field_type),
                            "description": field_info.description or f"The {field} of the response"
                        }
                        for field, field_info in output_model.model_fields.items()
                        if (field_type := output_model.__annotations__.get(field)) is not None
                    },
                    "required": list(output_model.model_fields.keys())
                }
            }

            # List of parameter sets to retry the API call
            retry_params = [
                {"prompt": prompt, "function_declarations": [function_declaration]},
                {"prompt": prompt, "temperature": 0.2, "function_declarations": [function_declaration]},
                {"contents": prompt, "function_declarations": [function_declaration]},
            ]

            response = None
            for params in retry_params:
                try:
                    response = await self.model.generate_content_async(**params)
                    if response:  # if a response was returned successfully, exit the retry loop
                        break
                except Exception as e:
                    logger.warning(f"Attempt with params {params} failed: {e}")

            if not response:
                logger.error("All attempts to call Gemini function failed.")
                return {"error": "Gemini API Error: All attempts failed."}

            # Process response using the helper method
            result = self._extract_function_call_result(response, f"extract_{model_name}")

            # Validate the result with the Pydantic model
            if result and "error" not in result:
                try:
                    validated_data = output_model(**result)
                    logger.debug(f"Gemini response validated successfully for {model_name}.")
                    return validated_data.model_dump()  # Return validated dict
                except Exception as pydantic_error:
                    logger.error(f"Gemini response failed Pydantic validation for {model_name}: {pydantic_error}\nRaw args: {result}")
                    return {"error": f"Gemini response validation failed: {pydantic_error}", "raw_args": result}

            response_text = response.text if hasattr(response, 'text') else 'No text available'
            logger.warning(f"Function call extraction failed: {result.get('error') if result else 'No result'}. Response text: {response_text[:200]}")
            return self._extract_json_from_text(response_text, output_model)

        except Exception as e:
            logger.error(f"Error in Gemini function call: {e}", exc_info=True)
            return {"error": f"Gemini API Error: {e}"}

    def _extract_json_from_text(self, text: str, output_model: BaseModel) -> Dict[str, Any]:
        """Extract and validate JSON from text response."""
        if not text:
            return {"error": "No text response available for JSON extraction"}

        try:
            # Look for JSON-like content in the response text
            json_match = re.search(r'\{.*\}', text, re.DOTALL | re.MULTILINE)
            if json_match:
                json_str = json_match.group(0)
                # Clean potential markdown
                json_str = re.sub(r'^```json\s*', '', json_str, flags=re.IGNORECASE)
                json_str = re.sub(r'\s*```$', '', json_str)
                json_str = json_str.strip()

                data = json.loads(json_str)

                # Try to validate with our model
                try:
                    validated_data = output_model(**data)
                    logger.info(f"Successfully extracted and validated JSON from text response")
                    return validated_data.model_dump()
                except Exception as e_pydantic:
                    logger.warning(f"Extracted JSON couldn't be validated by Pydantic: {e_pydantic}")
                    return {"error": f"Extracted JSON validation failed: {e_pydantic}", "extracted_json": data}
            else:
                logger.warning(f"No JSON block found in text response")

        except json.JSONDecodeError as e_json:
            logger.warning(f"Failed to parse extracted JSON from text: {e_json}")
        except Exception as e_fallback:
            logger.warning(f"Error during text JSON fallback: {e_fallback}")

        return {"error": "Could not extract valid JSON from text response"}

    def _pydantic_to_gemini_type(self, field_type: Any) -> str:
        """Maps Pydantic/Python types to JSON Schema types."""
        origin = getattr(field_type, "__origin__", None)
        if origin == list or origin == List:
            return "array"
        elif field_type == str:
            return "string"
        elif field_type == int:
            return "integer"
        elif field_type == float:
            return "number"
        elif field_type == bool:
            return "boolean"
        else:
            return "string" # Default fallback

    def _extract_value_from_proto(self, proto_value) -> Any:
        """Helper method to extract values from protobuf Value objects."""
        if hasattr(proto_value, 'string_value') and proto_value.string_value is not None:
            return proto_value.string_value
        elif hasattr(proto_value, 'number_value') and proto_value.number_value is not None:
            return proto_value.number_value
        elif hasattr(proto_value, 'bool_value') and proto_value.bool_value is not None:
            return proto_value.bool_value
        elif hasattr(proto_value, 'list_value') and proto_value.list_value is not None:
            return [self._extract_value_from_proto(item) for item in proto_value.list_value.values]
        elif hasattr(proto_value, 'struct_value') and proto_value.struct_value is not None:
            return {k: self._extract_value_from_proto(v) for k, v in proto_value.struct_value.items()}
        else:
            # Use string representation as a fallback
            return str(proto_value)

    async def analyze_anomaly(self, anomaly_context: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze an anomaly using Gemini AI with structured output."""
        prompt = f"""
        Analyze this Kubernetes anomaly based on the provided context. Focus on identifying the most likely root cause,
        assessing the potential impact and severity, and providing actionable recommendations for investigation.

        Context:
        {json.dumps(anomaly_context, indent=2, default=str)}

        Provide your analysis using the 'AnomalyAnalysis' structure.
        """
        result = await self._call_gemini_with_function(prompt, AnomalyAnalysis)
        return result or {"error": "Gemini analysis failed or service unavailable."}


    async def suggest_remediation(self, anomaly_context: Dict[str, Any]) -> Dict[str, Any]:
        """Suggest remediation actions with structured output."""
        prompt = f"""
        Based on the following Kubernetes anomaly context, suggest appropriate remediation actions.
        Focus on SAFE, common Kubernetes operations formatted as command strings (e.g., 'operation_name param1=value param2=value').
        Consider the likely root cause, severity, affected resources, potential risks, and expected outcome.

        Context:
        {json.dumps(anomaly_context, indent=2, default=str)}

        Provide your suggestions using the 'RemediationSteps' structure. Ensure 'steps' contains only the command strings.
        """
        result = await self._call_gemini_with_function(prompt, RemediationSteps)
        if result and "error" not in result:
            return result
        else:
            return result or {"error": "Gemini remediation suggestion failed or service unavailable."}

    # NEW METHODS

    async def prioritize_remediation_actions(self, anomaly_context: Dict[str, Any], suggested_commands: List[str],
                                            available_templates: Dict[str, Dict[str, Any]],
                                            dependency_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Prioritize remediation actions based on impact, dependencies, and effectiveness."""
        if not self.model:
            return {"error": "Gemini service disabled"}

        prompt = f"""
        Analyze the following Kubernetes anomaly and suggested remediation commands to prioritize them based on:
        1. Potential impact (minimize disruption)
        2. Effectiveness for the issue
        3. Speed of resolution
        4. Dependencies between services
        5. Criticality of affected components

        When relevant, convert raw commands to template-based actions (in RemediationAction format) and suggest phased/incremental
        approaches when appropriate (e.g., for scaling operations, suggest incremental steps with verification).

        Anomaly Context:
        {json.dumps(anomaly_context, indent=2, default=str)}

        Available Command Templates:
        {json.dumps(available_templates, indent=2, default=str)}

        Raw Suggested Commands:
        {json.dumps(suggested_commands, indent=2, default=str)}

        Dependencies Information (if available):
        {json.dumps(dependency_info or {}, indent=2, default=str)}

        Provide your prioritized remediation plan using the 'PrioritizedRemediation' structure.
        Favor incremental/phased approaches when dealing with scaling or resource-intensive changes.
        """

        result = await self._call_gemini_with_function(prompt, PrioritizedRemediation)
        return result or {"error": "Gemini prioritization failed or service unavailable."}

    async def generate_promql_queries(self, cluster_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get PromQL queries directly from Prometheus based on available metrics."""
        logger.info(f"Getting PromQL queries for cluster with {cluster_context.get('nodes', {}).get('count', 0)} nodes " +
                    f"and {sum(cluster_context.get('workloads', {}).values())} workloads")

        # Skip Gemini completely and go directly to Prometheus
        return await self._fetch_prometheus_example_queries(cluster_context)

    async def _fetch_prometheus_example_queries(self, cluster_context: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch example queries directly from Prometheus to use as fallbacks."""
        import aiohttp
        from urllib.parse import quote
        import asyncio

        logger.info("Fetching queries from Prometheus")

        # Try to connect to Prometheus at localhost:9090
        # These are common and useful Prometheus queries for Kubernetes monitoring
        prometheus_queries = []

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                # First, try to get the list of metrics from Prometheus
                async with session.get("http://localhost:9090/api/v1/label/__name__/values") as response:
                    if response.status == 200:
                        metrics_data = await response.json()
                        available_metrics = metrics_data.get("data", [])
                        logger.info(f"Found {len(available_metrics)} metrics in Prometheus")

                        # Build queries based on available metrics
                        kubernetes_metric_patterns = [
                            "container_cpu", "container_memory", "kube_pod",
                            "kube_deployment", "kube_node", "node_cpu",
                            "node_memory", "apiserver", "etcd", "kubelet",
                            "container_network", "kube_hpa", "kube_statefulset",
                            "kube_daemonset", "kube_service", "kube_ingress",
                            "kube_persistentvolume", "container_fs"
                        ]

                        # Filter metrics that match Kubernetes patterns
                        k8s_metrics = [m for m in available_metrics if any(pattern in m for pattern in kubernetes_metric_patterns)]
                        logger.info(f"Found {len(k8s_metrics)} Kubernetes-related metrics in Prometheus")

                        # Define categories for organizing queries
                        query_categories = {
                            "pod_resource_usage": [],
                            "node_resource_usage": [],
                            "deployment_health": [],
                            "pod_health": [],
                            "node_health": [],
                            "network": [],
                            "storage": [],
                            "control_plane": []
                        }

                        # Check for specific important metrics and build appropriate queries
                        # Pod CPU usage
                        if any("container_cpu_usage_seconds_total" in m for m in k8s_metrics):
                            query_categories["pod_resource_usage"].extend([
                                "sum(rate(container_cpu_usage_seconds_total{container!='POD',container!=''}[5m])) by (namespace, pod)",
                                "sum(rate(container_cpu_usage_seconds_total{container!='POD',container!=''}[5m])) by (namespace)",
                                "topk(10, sum(rate(container_cpu_usage_seconds_total{container!='POD',container!=''}[5m])) by (namespace, pod))"
                            ])

                        # Pod Memory usage
                        if any("container_memory_usage_bytes" in m for m in k8s_metrics):
                            query_categories["pod_resource_usage"].extend([
                                "sum(container_memory_usage_bytes{container!='POD',container!=''}) by (namespace, pod)",
                                "sum(container_memory_usage_bytes{container!='POD',container!=''}) by (namespace)",
                                "topk(10, sum(container_memory_usage_bytes{container!='POD',container!=''}) by (namespace, pod))"
                            ])

                        # Node CPU usage
                        if any("node_cpu_seconds_total" in m for m in k8s_metrics):
                            query_categories["node_resource_usage"].extend([
                                "sum(rate(node_cpu_seconds_total{mode!='idle'}[5m])) by (instance)",
                                "1 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) by (instance)",
                                "sum(rate(node_cpu_seconds_total{mode!='idle'}[5m]))"
                            ])

                        # Node memory usage
                        if any("node_memory_MemTotal_bytes" in m for m in k8s_metrics):
                            query_categories["node_resource_usage"].extend([
                                "sum(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) by (instance)",
                                "(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100",
                                "sum(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes) * 100"
                            ])

                        # Deployment status
                        if any("kube_deployment_status_replicas" in m for m in k8s_metrics):
                            query_categories["deployment_health"].extend([
                                "sum(kube_deployment_status_replicas_unavailable) by (namespace, deployment)",
                                "kube_deployment_status_replicas_available / kube_deployment_status_replicas",
                                "sum(kube_deployment_status_replicas_unavailable) > 0"
                            ])

                        # Pod status
                        if any("kube_pod_status_phase" in m for m in k8s_metrics):
                            query_categories["pod_health"].extend([
                                "sum(kube_pod_status_phase{phase='Failed'}) by (namespace)",
                                "sum(kube_pod_status_phase{phase='Pending'}) by (namespace)",
                                "sum(kube_pod_status_phase{phase!='Running',phase!='Succeeded'}) by (namespace)"
                            ])

                        # Container restarts
                        if any("kube_pod_container_status_restarts_total" in m for m in k8s_metrics):
                            query_categories["pod_health"].extend([
                                "sum(rate(kube_pod_container_status_restarts_total[1h])) by (namespace, pod)",
                                "topk(10, sum(rate(kube_pod_container_status_restarts_total[1h])) by (namespace, pod))",
                                "sum(changes(kube_pod_container_status_restarts_total[24h])) by (namespace, pod) > 5"
                            ])

                        # Disk usage
                        if any("node_filesystem_avail_bytes" in m for m in k8s_metrics):
                            query_categories["storage"].extend([
                                "sum(node_filesystem_size_bytes - node_filesystem_avail_bytes) by (instance, mountpoint)",
                                "(node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes * 100 > 80",
                                "sum by(instance) ((node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes * 100)"
                            ])

                        # Network
                        if any("container_network_receive_bytes_total" in m for m in k8s_metrics):
                            query_categories["network"].extend([
                                "sum(rate(container_network_receive_bytes_total[5m])) by (namespace, pod)",
                                "sum(rate(container_network_transmit_bytes_total[5m])) by (namespace, pod)",
                                "sum(rate(container_network_receive_errors_total[5m])) by (namespace, pod) > 0"
                            ])

                        # Control plane metrics
                        if any("apiserver_request_duration_seconds_bucket" in m for m in k8s_metrics):
                            query_categories["control_plane"].extend([
                                "histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket[5m])) by (le, verb, resource))",
                                "sum(rate(apiserver_request_total{code=~\"5..\"}[5m]))",
                                "sum(rate(apiserver_request_total[5m])) by (resource, verb, code)"
                            ])

                        if any("etcd_server_leader_changes_seen_total" in m for m in k8s_metrics):
                            query_categories["control_plane"].extend([
                                "rate(etcd_server_leader_changes_seen_total[1h])",
                                "histogram_quantile(0.99, sum(rate(etcd_disk_backend_commit_duration_seconds_bucket[5m])) by (le))",
                                "etcd_server_has_leader"
                            ])

                        # HPA metrics
                        if any("kube_hpa_status_current_replicas" in m for m in k8s_metrics):
                            query_categories["deployment_health"].extend([
                                "kube_hpa_status_current_replicas / kube_hpa_spec_max_replicas",
                                "kube_hpa_status_current_replicas / kube_hpa_spec_min_replicas",
                                "kube_hpa_status_current_replicas != kube_hpa_spec_target_replicas"
                            ])

                        # Node health
                        if any("kube_node_status_condition" in m for m in k8s_metrics):
                            query_categories["node_health"].extend([
                                "sum(kube_node_status_condition{status='true',condition='Ready'}) / count(kube_node_info) * 100",
                                "kube_node_status_condition{status='true',condition='DiskPressure'}",
                                "kube_node_status_condition{status='true',condition='MemoryPressure'}"
                            ])

                        # Create flatten list of all queries to validate
                        queries_to_try = []
                        for category, category_queries in query_categories.items():
                            queries_to_try.extend(category_queries)

                        # Test each query to see if it returns results
                        for query in queries_to_try:
                            try:
                                # URL encode the query
                                encoded_query = quote(query)
                                async with session.get(f"http://localhost:9090/api/v1/query?query={encoded_query}") as query_response:
                                    if query_response.status == 200:
                                        query_data = await query_response.json()
                                        if query_data.get("status") == "success" and query_data.get("data", {}).get("result"):
                                            # This query returned actual data
                                            prometheus_queries.append(query)
                                            logger.info(f"Query validated in Prometheus: {query}")
                            except Exception as query_error:
                                logger.warning(f"Error testing query {query}: {query_error}")

                # If we couldn't get any queries from Prometheus, use fallback queries
                if not prometheus_queries:
                    logger.warning("No queries could be validated in Prometheus, using standard fallbacks")
        except Exception as e:
            logger.warning(f"Failed to connect to Prometheus: {e}")

        # If we couldn't get any queries from Prometheus, use fallback queries
        if not prometheus_queries:
            # Define comprehensive fallback queries by category
            fallback_queries = {
                "pod_resource_usage": [
                    'sum(rate(container_cpu_usage_seconds_total{container!="POD",container!=""}[5m])) by (namespace, pod)',
                    'sum(container_memory_usage_bytes{container!="POD",container!=""}) by (namespace, pod)',
                    'topk(10, sum(rate(container_cpu_usage_seconds_total{container!="POD",container!=""}[5m])) by (namespace, pod))'
                ],
                "node_resource_usage": [
                    'sum(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (instance)',
                    'sum(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) by (instance)',
                    '(node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes * 100 > 80'
                ],
                "deployment_health": [
                    'sum(kube_deployment_status_replicas_unavailable) by (namespace, deployment) > 0',
                    'kube_deployment_status_replicas_available / kube_deployment_status_replicas < 1',
                    'sum(kube_deployment_status_replicas_unavailable) by (deployment)'
                ],
                "pod_health": [
                    'sum(kube_pod_status_phase{phase="Failed"}) by (namespace) > 0',
                    'sum(rate(kube_pod_container_status_restarts_total[1h])) by (namespace, pod) > 0',
                    'sum(kube_pod_status_phase{phase!="Running",phase!="Succeeded"}) by (namespace)'
                ],
                "network": [
                    'sum(rate(container_network_receive_bytes_total[5m])) by (namespace, pod)',
                    'sum(rate(container_network_transmit_bytes_total[5m])) by (namespace, pod)'
                ]
            }

            # Flatten the fallback queries
            prometheus_queries = []
            for category, queries in fallback_queries.items():
                prometheus_queries.extend(queries)

        # Structure the response
        categorized_queries = {}

        # Attempt to categorize the queries for better organization
        for query in prometheus_queries:
            category = "other"

            if "container_cpu" in query or "container_memory" in query:
                category = "pod_resource_usage"
            elif "node_cpu" in query or "node_memory" in query:
                category = "node_resource_usage"
            elif "kube_deployment" in query or "kube_hpa" in query:
                category = "deployment_health"
            elif "kube_pod" in query or "container_status" in query:
                category = "pod_health"
            elif "node_filesystem" in query or "persistentvolume" in query:
                category = "storage"
            elif "container_network" in query:
                category = "network"
            elif "apiserver" in query or "etcd" in query:
                category = "control_plane"
            elif "kube_node" in query:
                category = "node_health"

            if category not in categorized_queries:
                categorized_queries[category] = []

            categorized_queries[category].append(query)

        return {
            "queries": prometheus_queries,
            "query_categories": categorized_queries,
            "reasoning": "Generated PromQL queries based on available metrics in the local Prometheus instance",
            "generation_timestamp": datetime.utcnow().isoformat(),
            "is_prometheus_sourced": True,
            "cluster_context": cluster_context
        }

    def _extract_function_call_result(self, response, expected_function_name: str) -> Optional[Dict[str, Any]]:
        """Helper method to extract function call results from Gemini response."""
        try:
            # Check if we have valid candidates
            if not hasattr(response, 'candidates') or not response.candidates:
                return None

            for candidate in response.candidates:
                # Check for content and parts
                if not hasattr(candidate, 'content') or not candidate.content or not hasattr(candidate.content, 'parts'):
                    continue

                # Look for function call in parts
                for part in candidate.content.parts:
                    # Check if this part has a function call
                    if not hasattr(part, 'function_call') or not part.function_call:
                        continue

                    # Extract the function call
                    function_call = part.function_call

                    # Verify it's the function we expect
                    if hasattr(function_call, 'name') and function_call.name != expected_function_name:
                        logger.warning(f"Expected function {expected_function_name} but got {function_call.name}")
                        continue

                    # Extract arguments
                    if not hasattr(function_call, 'args'):
                        continue

                    args = function_call.args
                    result = {}

                    # Process different types of args
                    if hasattr(args, 'items'):  # Handle Struct type
                        for key, value in args.items():
                            result[key] = self._extract_value_from_proto(value)
                    elif isinstance(args, dict):  # Handle dict
                        result = args
                    else:
                        # Try to convert to string and parse as JSON as last resort
                        try:
                            result = json.loads(str(args))
                        except:
                            logger.error(f"Could not parse function call args: {args}")
                            return None

                    return result

            return None
        except Exception as e:
            logger.error(f"Error extracting function call result: {e}")
            return None

    async def suggest_resource_optimizations(self, resource_metrics_history: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze resource usage patterns and suggest optimizations."""
        if not self.model:
            return {"error": "Gemini service disabled"}

        prompt = f"""
        Analyze the historical resource usage patterns for this Kubernetes workload and suggest optimal
        resource requests and limits. Consider:

        1. Peak usage vs. typical usage patterns
        2. Daily/weekly patterns if visible
        3. Growth trends
        4. Application requirements
        5. Safety margins for spikes

        Avoid over-provisioning while ensuring stability.

        Resource Metrics History:
        {json.dumps(resource_metrics_history, indent=2, default=str)}

        Provide your optimization suggestion using the 'ResourceOptimizationSuggestion' structure.
        """

        result = await self._call_gemini_with_function(prompt, ResourceOptimizationSuggestion)
        return result or {"error": "Gemini resource optimization suggestion failed or service unavailable."}

    async def generate_rca_report(self, anomaly_event: Dict[str, Any], remediation_attempts: List[Dict[str, Any]],
                                 all_metrics: Dict[str, List[Dict[str, Any]]], logs: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Generate a detailed Root Cause Analysis report for a resolved issue."""
        if not self.model:
            return {"error": "Gemini service disabled"}

        prompt = f"""
        Generate a comprehensive Root Cause Analysis (RCA) report for this resolved Kubernetes incident.

        Include:
        - A clear summary of the issue
        - Detailed analysis of the root cause
        - Timeline from detection to resolution
        - Metrics analysis
        - Remediation steps taken and their effectiveness
        - Recommendations to prevent recurrence

        Anomaly Event:
        {json.dumps(anomaly_event, indent=2, default=str)}

        Remediation Attempts:
        {json.dumps(remediation_attempts, indent=2, default=str)}

        Metrics Data:
        {json.dumps(all_metrics, indent=2, default=str)}

        Logs (if available):
        {json.dumps(logs or {}, indent=2, default=str)}

        Provide your report using the 'RootCauseAnalysisReport' structure.
        """

        result = await self._call_gemini_with_function(prompt, RootCauseAnalysisReport)
        return result or {"error": "Gemini RCA report generation failed or service unavailable."}

    async def verify_remediation_success(
        self,
        anomaly_event: Dict[str, Any], # Use dict representation of AnomalyEvent
        remediation_attempt: Dict[str, Any], # Use dict representation of RemediationAttempt
        current_metrics: List[Dict[str, Any]], # Post-remediation metrics window
        current_k8s_status: Optional[Dict[str, Any]] = None # Optional post-remediation K8s status
        ) -> Dict[str, Any]:
        """Verify remediation success using Gemini."""
        if not self.model: return {"error": "Gemini service disabled"}

        # Construct context carefully
        verification_context = {
            "original_anomaly": {
                "entity_id": anomaly_event.get("entity_id"),
                "entity_type": anomaly_event.get("entity_type"),
                "namespace": anomaly_event.get("namespace"),
                "detected_at": anomaly_event.get("detection_timestamp"),
                "original_score": anomaly_event.get("anomaly_score"),
                "original_analysis": anomaly_event.get("ai_analysis"),
                "triggering_metrics_snapshot": anomaly_event.get("metric_snapshot", [])[-1] if anomaly_event.get("metric_snapshot") else {}  # Just last known bad state
            },
            "remediation_action": {
                "command": remediation_attempt.get("command"),
                "parameters": remediation_attempt.get("parameters"),
                "executor": remediation_attempt.get("executor"),
                "timestamp": remediation_attempt.get("timestamp")
            },
            "current_state": {
                "timestamp": datetime.utcnow().isoformat(),
                "current_metrics_snapshot": current_metrics[-1] if current_metrics else None, # Latest metrics
                "current_kubernetes_resource_status": current_k8s_status # Optional direct status check
            }
        }

        prompt = f"""
        Assess if the attempted remediation action likely resolved the original Kubernetes anomaly.
        Consider the original issue (context provided), the action taken, and the current state (metrics, optional K8s status).
        Did the action address the likely root cause? Have the relevant metrics returned to a normal range? Is the resource healthy?

        Context for Verification:
        {json.dumps(verification_context, indent=2, default=str)}

        Provide your assessment using the 'VerificationResult' structure, indicating success/failure and reasoning.
        """
        result = await self._call_gemini_with_function(prompt, VerificationResult)
        return result or {"error": "Gemini verification failed or service unavailable.", "success": False, "reasoning": "AI verification failed.", "confidence": 0.0} # Default to failure on error

    async def batch_process_anomaly(self, anomaly_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process anomaly analysis and remediation suggestions in a single API call to reduce latency.

        Args:
            anomaly_context: Dictionary containing anomaly data including metrics, entity info, etc.

        Returns:
            Dictionary containing both analysis and remediation suggestions.
        """
        try:
            if not self.model or not self._is_initialized:
                return {"error": "Gemini service not initialized"}

            # Construct a prompt that asks for both analysis and remediation in one call
            prompt = f"""
            # Kubernetes Anomaly Analysis and Remediation Task

            ## Anomaly Context:
            - Entity Type: {anomaly_context['entity_type']}
            - Entity ID: {anomaly_context['entity_id']}
            - Namespace: {anomaly_context.get('namespace', 'default')}
            - Anomaly Score: {anomaly_context.get('anomaly_score', 'N/A')}
            - Timestamp: {anomaly_context.get('timestamp', datetime.utcnow().isoformat())}

            ## Metrics at Time of Anomaly:
            {json.dumps(anomaly_context.get('metrics_snapshot', []), indent=2)}

            # Task:
            1. Analyze the anomaly and identify the probable root cause
            2. Suggest specific remediation steps for this anomaly (provide executable Kubernetes commands)

            Return your response in the following JSON format:
            ```json
            {
              "analysis": {
                "probable_cause": "string",
                "severity": "LOW|MEDIUM|HIGH|CRITICAL",
                "description": "string"
              },
              "remediation": {
                "steps": [
                  "kubectl command 1",
                  "kubectl command 2"
                ],
                "reasoning": "string"
              }
            }
            ```
            Ensure all remediation commands are valid kubectl or k8s management commands for the specific issue.
            """

            response = await self._generate_response(
                prompt,
                response_format={"type": "json_object"},
                temperature=0.2
            )

            if not response or not response.parts or not response.parts[0].text:
                return {"error": "Empty response from Gemini"}

            response_text = response.parts[0].text
            response_data = json.loads(response_text)

            # Extract and format results
            result = {
                "analysis": response_data.get("analysis", {}),
                "remediation": {
                    "steps": response_data.get("remediation", {}).get("steps", []),
                    "reasoning": response_data.get("remediation", {}).get("reasoning", "")
                }
            }

            return result
        except json.JSONDecodeError:
            return {"error": "Invalid JSON response from Gemini"}
        except Exception as e:
            logger.error(f"Error in batch_process_anomaly: {str(e)}", exc_info=True)
            return {"error": f"Failed to process anomaly: {str(e)}"}

    async def _generate_response(self, prompt: str, **kwargs) -> Any:
        """
        Generate a response from Gemini model with provided parameters.

        Args:
            prompt: The text prompt to send to Gemini
            **kwargs: Additional parameters to pass to the Gemini generate_content_async method

        Returns:
            The Gemini response object
        """
        if not self.model:
            logger.warning("Gemini service not configured or unavailable.")
            return None

        try:
            # Attempt to generate content with the provided parameters
            response = await self.model.generate_content_async(prompt, **kwargs)
            return response
        except Exception as e:
            logger.error(f"Error generating Gemini response: {e}", exc_info=True)
            return None
