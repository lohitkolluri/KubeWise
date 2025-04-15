from typing import Dict, Any, List, Optional, Type, Union, Callable
import json
from loguru import logger
from google import genai
from pydantic import BaseModel, Field
from datetime import datetime
import re
from app.core.config import settings

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
        self.client = None
        self.model = None
        self._is_initialized = False
        self._api_key_missing = False
        self.generation_config = None
        self.chat_sessions = {}

        if settings.GEMINI_API_KEY:
            try:
                # Initialize using the new Google Gen AI SDK pattern
                self.client = genai.Client(api_key=settings.GEMINI_API_KEY)

                # Using the latest model version
                model_name = 'gemini-2.0-flash'  # Upgrading to newer model version

                # Set default generation configuration
                self.generation_config = genai.types.GenerateContentConfig(
                    temperature=0.2,
                    top_p=0.95,
                    top_k=40,
                    max_output_tokens=4096
                )

                self._is_initialized = True
                logger.info(f"Gemini service initialized successfully with model {model_name}")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini service: {e}")
        else:
            self._api_key_missing = True
            logger.warning("Gemini service disabled: GEMINI_API_KEY not set.")

    def is_api_key_missing(self) -> bool:
        """Check if the API key is missing."""
        return self._api_key_missing

    async def _call_gemini_with_function(self, prompt: str, output_model: BaseModel) -> Optional[Dict[str, Any]]:
        """Helper to call Gemini API with function calling using the updated approach."""
        if not self.model:
            logger.warning("Gemini service not configured or unavailable.")
            return {"error": "Gemini service not configured or unavailable."}

        try:
            model_name = output_model.__name__

            # Create function declaration for Gemini
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

            # 1. Try with direct function calling approach
            try:
                # Fix: Use the correct tools structure format for the Gemini API
                tools = [{
                    "function_declarations": [function_declaration]
                }]

                # Create proper generation config
                generation_config = {
                    "temperature": 0.2,
                    "max_output_tokens": 4096
                }

                # Fix: Pass tools as a list with a single item containing function_declarations
                response = await self.model.generate_content_async(
                    contents=prompt,
                    generation_config=generation_config,
                    tools=tools  # Fix: Pass tools as a list with proper structure
                )

                # Process the response for function calling
                function_call_response = self._extract_function_response(response, f"extract_{model_name}")

                if function_call_response and "error" not in function_call_response:
                    # Validate with Pydantic
                    try:
                        validated_data = output_model(**function_call_response)
                        logger.info(f"Gemini response validated successfully for {model_name}.")
                        return validated_data.model_dump()
                    except Exception as validation_error:
                        logger.error(f"Validation error: {validation_error}")
                        # Try to construct partial valid response
                        return self._construct_partial_response(function_call_response, output_model)
                else:
                    logger.warning(f"No valid function call found in response")

            except Exception as e:
                logger.warning(f"Primary Gemini function call failed: {e}")

                # 2. Fallback: try with simple text generation
                try:
                    # Fallback to simpler text response
                    simple_generation_config = {
                        "temperature": 0.2,
                        "max_output_tokens": 4096
                    }

                    response = await self.model.generate_content_async(
                        contents=f"{prompt}\n\nRespond with a JSON object in this exact format: {output_model.model_json_schema()}",
                        generation_config=simple_generation_config
                    )

                    # Try to extract JSON from the text response
                    if hasattr(response, 'text'):
                        json_response = self._extract_json_from_text(response.text, output_model)
                        if json_response and "error" not in json_response:
                            return json_response
                except Exception as fallback_error:
                    logger.error(f"Fallback Gemini call also failed: {fallback_error}")

            # 3. Create minimal response if all else fails
            logger.warning(f"All Gemini API approaches failed. Creating minimal response for {model_name}")
            return self._create_minimal_response(output_model)

        except Exception as e:
            logger.error(f"Error in Gemini function call: {e}", exc_info=True)
            return {"error": f"Gemini API Error: {e}"}

    def _extract_function_call(self, response) -> Optional[Dict[str, Any]]:
        """Extract function call information from a response."""
        try:
            if not response or not hasattr(response, 'candidates') or not response.candidates:
                return None

            for candidate in response.candidates:
                if not hasattr(candidate, 'content') or not candidate.content:
                    continue

                # Check for parts in the content
                if hasattr(candidate.content, 'parts'):
                    for part in candidate.content.parts:
                        if hasattr(part, 'function_call') and part.function_call:
                            return {
                                "name": part.function_call.name,
                                "args": part.function_call.args
                            }

                # Also check for direct function_call attribute (may vary by API version)
                if hasattr(candidate.content, 'function_call') and candidate.content.function_call:
                    return {
                        "name": candidate.content.function_call.name,
                        "args": candidate.content.function_call.args
                    }

            return None

        except Exception as e:
            logger.error(f"Error extracting function call: {e}")
            return None

    def _construct_partial_response(self, data: Dict[str, Any], output_model: BaseModel) -> Dict[str, Any]:
        """Create a partial valid response from a partially invalid one."""
        try:
            partial_data = {}
            for field, value in data.items():
                if field in output_model.model_fields:
                    try:
                        # Basic type validation
                        field_type = output_model.__annotations__.get(field)

                        # Handle List types
                        if hasattr(field_type, "__origin__") and field_type.__origin__ == list:
                            if isinstance(value, list):
                                partial_data[field] = value
                        # Handle simple types
                        elif field_type in (str, int, float, bool):
                            if isinstance(value, field_type):
                                partial_data[field] = value
                        else:
                            # Just include it and hope for the best
                            partial_data[field] = value
                    except:
                        pass  # Skip this field

            if partial_data:
                logger.info(f"Created partial valid response with fields: {list(partial_data.keys())}")
                return partial_data
            else:
                return {"error": "Could not create valid partial response"}
        except Exception as e:
            logger.error(f"Error creating partial response: {e}")
            return {"error": f"Error creating partial response: {e}"}

    def _create_minimal_response(self, output_model: BaseModel) -> Dict[str, Any]:
        """Create a minimal valid response based on model requirements."""
        try:
            minimal_response = {}
            for field, field_info in output_model.model_fields.items():
                if field_info.is_required:
                    field_type = output_model.__annotations__.get(field)

                    # Set default values based on type
                    if field_type == str:
                        minimal_response[field] = "No information available"
                    elif field_type == float:
                        minimal_response[field] = 0.5  # Default confidence
                    elif field_type == bool:
                        minimal_response[field] = False
                    elif hasattr(field_type, "__origin__") and field_type.__origin__ == list:
                        minimal_response[field] = ["No data available"]
                    elif field_type == Dict[str, Any] or field_type == Dict[str, str]:
                        minimal_response[field] = {"info": "No data available"}
                    else:
                        minimal_response[field] = None

            logger.info(f"Created minimal response with required fields")
            return minimal_response
        except Exception as e:
            logger.error(f"Error creating minimal response: {e}")
            return {"error": "Could not create minimal response", "details": str(e)}

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

    async def create_chat_session(self,
                                 system_instruction: Optional[str] = None,
                                 history_id: Optional[str] = None) -> str:
        """
        Create a new chat session with optional system instructions.

        Args:
            system_instruction: Optional system instruction to guide the conversation
            history_id: Optional ID to use for the chat session

        Returns:
            Chat session ID
        """
        if not self.model or not self._is_initialized:
            logger.warning("Gemini service not initialized")
            return None

        try:
            # Generate a random ID if none provided
            if not history_id:
                history_id = f"chat_{datetime.now().strftime('%Y%m%d%H%M%S')}_{id(self):x}"

            # Initialize a new chat session
            # For Google Generative AI 0.3.2, we use a simpler chat session tracking
            self.chat_sessions[history_id] = {
                "created_at": datetime.now().isoformat(),
                "messages": [],
                "system_instruction": system_instruction
            }

            logger.info(f"Created chat session with ID: {history_id}")
            return history_id

        except Exception as e:
            logger.error(f"Failed to create chat session: {e}")
            return None

    async def chat_message(self,
                          message: str,
                          history_id: str = None,
                          system_instruction: Optional[str] = None) -> Dict[str, Any]:
        """
        Send a message in a chat session and get a response.

        Args:
            message: The user message to send
            history_id: The chat session ID
            system_instruction: Optional system instruction (if creating a new chat)

        Returns:
            Dictionary containing the response text and metadata
        """
        if not self.model or not self._is_initialized:
            return {"error": "Gemini service not initialized"}

        try:
            # Create a new chat session if none exists or ID not found
            if not history_id or history_id not in self.chat_sessions:
                history_id = await self.create_chat_session(system_instruction)
                if not history_id:
                    return {"error": "Failed to create chat session"}

            chat_history = self.chat_sessions[history_id]

            # Build the full conversation context
            chat_context = []
            for msg in chat_history["messages"]:
                chat_context.append({"role": msg["role"], "parts": [{"text": msg["content"]}]})

            # Add the new message
            chat_context.append({"role": "user", "parts": [{"text": message}]})

            # Get system instruction if present
            system_instruction_text = chat_history.get("system_instruction")

            # Generate the response
            generation_config = genai.types.GenerationConfig(
                temperature=0.2,
                top_p=0.95,
                top_k=40,
                max_output_tokens=2048
            )

            safety_settings = [
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_ONLY_HIGH"
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_ONLY_HIGH"
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_ONLY_HIGH"
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_ONLY_HIGH"
                }
            ]

            if system_instruction_text:
                response = await self.model.generate_content_async(
                    contents=chat_context,
                    generation_config=generation_config,
                    safety_settings=safety_settings,
                    system_instruction=system_instruction_text
                )
            else:
                response = await self.model.generate_content_async(
                    contents=chat_context,
                    generation_config=generation_config,
                    safety_settings=safety_settings
                )

            response_text = response.text if hasattr(response, 'text') else "No response text available"

            # Add to history
            self.chat_sessions[history_id]["messages"].append({
                "role": "user",
                "content": message,
                "timestamp": datetime.now().isoformat()
            })

            self.chat_sessions[history_id]["messages"].append({
                "role": "model",
                "content": response_text,
                "timestamp": datetime.now().isoformat()
            })

            return {
                "text": response_text,
                "history_id": history_id,
                "message_count": len(self.chat_sessions[history_id]["messages"])
            }

        except Exception as e:
            logger.error(f"Error in chat message: {e}")
            return {"error": f"Failed to process chat message: {str(e)}"}

    async def get_chat_history(self, history_id: str) -> List[Dict[str, Any]]:
        """Get the history of a chat session."""
        if history_id not in self.chat_sessions:
            return []

        return self.chat_sessions[history_id]["messages"]

    async def analyze_kubernetes_logs(self, logs: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Analyze Kubernetes logs for anomalies and issues.

        Args:
            logs: The logs to analyze
            context: Additional context about the resource

        Returns:
            Analysis results
        """
        if not self.model or not self._is_initialized:
            return {"error": "Gemini service not initialized"}

        # Truncate logs if too long (to avoid token limits)
        max_log_lines = 100  # Adjust based on your needs
        logs_truncated = False

        if logs.count('\n') > max_log_lines:
            logs_lines = logs.splitlines()
            logs = '\n'.join(logs_lines[-max_log_lines:])
            logs_truncated = True

        system_instruction = """
        You are a Kubernetes log analysis expert with deep expertise in identifying issues in
        container logs, system logs, and application logs. Analyze the provided logs for:
        1. Error patterns and exceptions
        2. Resource constraints (OOM, CPU throttling)
        3. Connectivity issues
        4. Security concerns
        5. Performance bottlenecks

        Provide concise, actionable analysis. Focus on what's most important.
        """

        prompt = f"""
        # Kubernetes Log Analysis Request

        ## Resource Context:
        {json.dumps(context or {}, indent=2, default=str)}

        ## Logs{' (truncated to last 100 lines)' if logs_truncated else ''}:
        ```
        {logs}
        ```

        Analyze these logs for potential issues and anomalies.
        Identify errors, warnings, and patterns that may indicate problems.
        If the logs look normal, say so.
        """

        try:
            response = await self.model.generate_content_async(
                contents=prompt,
                generation_config=self.generation_config,
                system_instruction=system_instruction
            )

            return {
                "analysis": response.text if hasattr(response, 'text') else "Analysis not available",
                "logs_truncated": logs_truncated
            }

        except Exception as e:
            logger.error(f"Error analyzing logs: {e}")
            return {"error": f"Failed to analyze logs: {str(e)}"}

    async def execute_code_for_kubernetes_analysis(self,
                                                 query: str,
                                                 context_data: Dict[str, Any],
                                                 include_libraries: bool = True) -> Dict[str, Any]:
        """
        Generate and execute Python code to analyze Kubernetes data.

        Args:
            query: The user's query or analysis request
            context_data: Dictionary of relevant Kubernetes data
            include_libraries: Whether to include Python libraries for analysis

        Returns:
            Results of code execution and analysis
        """
        if not self.model or not self._is_initialized:
            return {"error": "Gemini service not initialized"}

        # Format context data for code execution
        context_json = json.dumps(context_data, indent=2, default=str)

        libraries_prompt = """
        You can use the following libraries in your analysis:
        - pandas and numpy for data manipulation
        - matplotlib for plotting
        - seaborn for enhanced visualizations
        - sklearn for anomaly detection and clustering
        """ if include_libraries else ""

        prompt = f"""
        # Kubernetes Data Analysis Task

        I need you to analyze the following Kubernetes data:
        ```json
        {context_json}
        ```

        Specific request: {query}

        {libraries_prompt}

        Please generate Python code to:
        1. Load and parse the data
        2. Perform the requested analysis
        3. Generate visualizations if appropriate
        4. Provide clear insights

        I'll execute your code automatically with the code execution tool.
        """

        try:
            # Code execution tool setup
            tool = {"code_execution": {}}

            response = await self.model.generate_content_async(
                contents=prompt,
                generation_config=self.generation_config,
                tools=[tool]
            )

            # Extract results and code from the response
            results = {
                "summary": "",
                "code": "",
                "execution_result": "",
                "visualizations": []
            }

            if response and hasattr(response, 'candidates') and response.candidates:
                for candidate in response.candidates:
                    if hasattr(candidate, 'content') and candidate.content:
                        if hasattr(candidate.content, 'parts'):
                            for part in candidate.content.parts:
                                if hasattr(part, 'text') and part.text:
                                    results["summary"] += part.text
                                if hasattr(part, 'executable_code') and part.executable_code:
                                    results["code"] += part.executable_code.code
                                if hasattr(part, 'code_execution_result') and part.code_execution_result:
                                    results["execution_result"] += part.code_execution_result.output
                                if hasattr(part, 'inline_data') and part.inline_data:
                                    results["visualizations"].append({
                                        "mime_type": part.inline_data.mime_type,
                                        "data": part.inline_data.data
                                    })

            return results

        except Exception as e:
            logger.error(f"Error in code execution: {e}")
            return {"error": f"Failed to execute code analysis: {str(e)}"}

    # Enhance existing methods with system instructions and structured output

    async def analyze_anomaly(self, anomaly_context: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze an anomaly or failure event."""
        logger.debug(f"Analyzing anomaly for {anomaly_context.get('entity_type')}/{anomaly_context.get('entity_id')}")

        if not self.model or not self._is_initialized:
            return {"error": "Gemini service not initialized"}

        is_critical = anomaly_context.get("is_critical", False)
        event_type = "critical failure" if is_critical else "anomaly"

        # Optimize metric data to reduce token usage
        metrics_snapshot = anomaly_context.get('metrics_snapshot', [])
        optimized_metrics = []

        # Check if we have prediction data to include in the analysis
        prediction_data = anomaly_context.get('prediction_data', None)
        has_predictions = prediction_data is not None and len(prediction_data) > 0

        # Add system instruction for better context
        system_instruction = """
        You are a Kubernetes anomaly analysis expert with deep expertise in:
        1. Root cause analysis of container and pod failures
        2. Performance troubleshooting for Kubernetes workloads
        3. Resource constraint identification
        4. Detection of cascading failures
        5. Security incident analysis
        6. Network and storage issues
        7. API server and etcd performance problems
        8. Pod scheduling and resource allocation issues

        Analyze Kubernetes anomalies or critical failures with precision and depth.
        Focus on actionable insights rather than generic statements.
        Prioritize urgent remediation steps for critical failures.

        When analyzing metrics:
        1. Look for patterns across multiple metrics
        2. Consider temporal relationships between events
        3. Check for resource constraints and limits
        4. Analyze network and storage performance
        5. Consider API server and etcd health
        6. Look for pod scheduling issues
        7. Check for container lifecycle problems
        8. Analyze node conditions and resource pressure
        """

        # Prepare prediction context if available
        prediction_context = ""
        if has_predictions:
            prediction_context = f"""
            ## Prediction Data:
            - Predicted Failures: {prediction_data.get('predicted_failures', [])}
            - Forecast: {prediction_data.get('forecast', {})}
            - Recommendation: {prediction_data.get('recommendation', 'monitor')}
            """

        # Group metrics by category for better analysis
        metrics_by_category = {
            "resource_usage": [],
            "container_status": [],
            "node_conditions": [],
            "network": [],
            "storage": [],
            "api_server": [],
            "scheduling": []
        }

        for metric in metrics_snapshot:
            query = metric.get("query", "").lower()
            if any(term in query for term in ["cpu", "memory", "resource"]):
                metrics_by_category["resource_usage"].append(metric)
            elif any(term in query for term in ["container", "pod", "phase"]):
                metrics_by_category["container_status"].append(metric)
            elif "node" in query:
                metrics_by_category["node_conditions"].append(metric)
            elif any(term in query for term in ["network", "receive", "transmit"]):
                metrics_by_category["network"].append(metric)
            elif any(term in query for term in ["disk", "fs", "volume"]):
                metrics_by_category["storage"].append(metric)
            elif "apiserver" in query:
                metrics_by_category["api_server"].append(metric)
            elif any(term in query for term in ["scheduling", "unschedulable"]):
                metrics_by_category["scheduling"].append(metric)

        # Format metrics by category
        formatted_metrics = []
        for category, metrics in metrics_by_category.items():
            if metrics:
                formatted_metrics.append(f"\n### {category.replace('_', ' ').title()} Metrics:")
                for metric in metrics:
                    query = metric.get("query", "")
                    values = metric.get("values", [])
                    if values:
                        last_value = values[-1][1] if isinstance(values[-1], list) else values[-1]
                        formatted_metrics.append(f"- {query}: {last_value}")

        prompt = f"""
        # Kubernetes {event_type.upper()} Analysis Task

        ## Context:
        Entity Type: {anomaly_context['entity_type']}
        Entity ID: {anomaly_context['entity_id']}
        Namespace: {anomaly_context.get('namespace', 'default')}
        Anomaly Score: {anomaly_context.get('anomaly_score', 'N/A')}
        Is Critical: {is_critical}

        ## Metrics Analysis:
        {chr(10).join(formatted_metrics)}

        {prediction_context}

        Analyze this {event_type} considering:
        1. Root cause analysis across all metric categories
        2. Potential impact on the system and dependencies
        3. Severity assessment based on multiple indicators
        4. Immediate recommendations with priority
        5. Assessment of prediction reliability (if prediction data is provided)
        6. Resource constraints and limits
        7. Network and storage performance
        8. API server and etcd health
        9. Pod scheduling and resource allocation
        10. Container lifecycle issues

        If this is a critical failure, focus on:
        - Immediate service impact
        - Potential cascading failures
        - Time-critical actions needed
        - Data loss risks
        - System stability concerns

        Provide your analysis using the AnomalyAnalysis structure.
        """

        logger.debug(f"Calling Gemini for anomaly analysis of {anomaly_context.get('entity_id')}")

        # Try with structured output first
        try:
            response = await self.model.generate_content_async(
                model=self.model.name,
                contents=prompt,
                generation_config=self.generation_config,
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=AnomalyAnalysis
            )

            if hasattr(response, 'text'):
                try:
                    data = json.loads(response.text)
                    validated_data = AnomalyAnalysis(**data)
                    logger.info(f"Structured output approach succeeded for anomaly analysis")
                    return validated_data.model_dump()
                except Exception as json_error:
                    logger.warning(f"Failed to parse JSON response: {json_error}")
        except Exception as e:
            logger.warning(f"Structured output approach failed: {e}")

        # Fall back to function calling approach
        return await self._call_gemini_with_function(prompt, AnomalyAnalysis)

    async def suggest_remediation(self, anomaly_context: Dict[str, Any], prioritize_critical: bool = False) -> Dict[str, Any]:
        """Suggest remediation actions with structured output."""
        if not self.model or not self._is_initialized:
            return {"error": "Gemini service not initialized"}

        # Enhance prompt based on criticality
        failure_context = "CRITICAL FAILURE" if prioritize_critical else "anomaly"
        priority_context = "This is a CRITICAL failure requiring immediate remediation." if prioritize_critical else ""

        # Group metrics by category for better analysis
        metrics_by_category = {
            "resource_usage": [],
            "container_status": [],
            "node_conditions": [],
            "network": [],
            "storage": [],
            "api_server": [],
            "scheduling": []
        }

        for metric in anomaly_context.get('metrics_snapshot', []):
            query = metric.get("query", "").lower()
            if any(term in query for term in ["cpu", "memory", "resource"]):
                metrics_by_category["resource_usage"].append(metric)
            elif any(term in query for term in ["container", "pod", "phase"]):
                metrics_by_category["container_status"].append(metric)
            elif "node" in query:
                metrics_by_category["node_conditions"].append(metric)
            elif any(term in query for term in ["network", "receive", "transmit"]):
                metrics_by_category["network"].append(metric)
            elif any(term in query for term in ["disk", "fs", "volume"]):
                metrics_by_category["storage"].append(metric)
            elif "apiserver" in query:
                metrics_by_category["api_server"].append(metric)
            elif any(term in query for term in ["scheduling", "unschedulable"]):
                metrics_by_category["scheduling"].append(metric)

        # Format metrics by category
        formatted_metrics = []
        for category, metrics in metrics_by_category.items():
            if metrics:
                formatted_metrics.append(f"\n### {category.replace('_', ' ').title()} Metrics:")
                for metric in metrics:
                    query = metric.get("query", "")
                    values = metric.get("values", [])
                    if values:
                        last_value = values[-1][1] if isinstance(values[-1], list) else values[-1]
                        formatted_metrics.append(f"- {query}: {last_value}")

        prompt = f"""
        # Kubernetes {failure_context} Remediation Task

        ## Context:
        {priority_context}
        Entity Type: {anomaly_context['entity_type']}
        Entity ID: {anomaly_context['entity_id']}
        Namespace: {anomaly_context.get('namespace', 'default')}
        Anomaly Score: {anomaly_context.get('anomaly_score', 'N/A')}

        ## Metrics Analysis:
        {chr(10).join(formatted_metrics)}

        ## Task:
        Suggest specific remediation commands to address this {failure_context}.
        For each suggested command, provide:
        1. The exact kubectl command to execute
        2. The expected outcome
        3. Potential risks
        4. Confidence level in the suggestion

        If the root cause is unclear, suggest diagnostic commands first:
        - kubectl describe pod/namespace
        - kubectl logs
        - kubectl get events
        - kubectl top pod/node
        - kubectl get pod -o wide

        For critical failures, prioritize:
        1. Immediate stability restoration
        2. Prevention of cascading failures
        3. Data preservation
        4. Service recovery

        For non-critical issues, consider:
        1. Root cause investigation
        2. Gradual remediation
        3. Performance optimization
        4. Resource efficiency

        Provide your suggestions using the RemediationSteps structure.
        """

        return await self._call_gemini_with_function(prompt, RemediationSteps)

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
        anomaly_event: Dict[str, Any],
        remediation_attempt: Dict[str, Any],
        current_metrics: List[Dict[str, Any]],
        current_k8s_status: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Verify remediation success using Gemini."""
        if not self.model:
            return {"error": "Gemini service disabled"}

        was_critical = anomaly_event.get("status") == "CriticalFailure"
        verification_context = {
            "original_issue": {
                "entity_id": anomaly_event.get("entity_id"),
                "entity_type": anomaly_event.get("entity_type"),
                "namespace": anomaly_event.get("namespace"),
                "detected_at": anomaly_event.get("detection_timestamp"),
                "original_score": anomaly_event.get("anomaly_score"),
                "was_critical": was_critical,
                "original_analysis": anomaly_event.get("ai_analysis"),
                "triggering_metrics": anomaly_event.get("metric_snapshot", [])[-1] if anomaly_event.get("metric_snapshot") else {}
            },
            "remediation_action": {
                "command": remediation_attempt.get("command"),
                "parameters": remediation_attempt.get("parameters"),
                "executor": remediation_attempt.get("executor"),
                "timestamp": remediation_attempt.get("timestamp")
            },
            "current_state": {
                "timestamp": datetime.utcnow().isoformat(),
                "metrics": current_metrics[-1] if current_metrics else None,
                "kubernetes_status": current_k8s_status
            }
        }

        prompt = f"""
        Assess if the attempted remediation action resolved the original {'critical failure' if was_critical else 'anomaly'}.

        ## Original Issue
        Type: {'CRITICAL FAILURE' if was_critical else 'Anomaly'}
        Entity: {verification_context['original_issue']['entity_type']}/{verification_context['original_issue']['entity_id']}
        Namespace: {verification_context['original_issue']['namespace']}

        ## Detailed Context:
        {json.dumps(verification_context, indent=2, default=str)}

        Consider:
        1. Has the immediate issue been resolved?
        2. Are the metrics showing healthy patterns?
        3. Is the Kubernetes resource in a stable state?
        4. For critical failures: Are there any signs of lingering instability?

        Provide your verification using the VerificationResult structure.
        """

        return await self._call_gemini_with_function(prompt, VerificationResult)

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

    async def analyze_metrics_trend(self,
                                   metric_name: str,
                                   metric_values: List[float],
                                   timestamps: List[str]) -> Dict[str, Any]:
        """
        Analyze trend in a Kubernetes metric over time.

        Args:
            metric_name: Name of the metric
            metric_values: List of metric values over time
            timestamps: List of corresponding timestamps

        Returns:
            Dictionary with trend analysis
        """
        if not self.model or not self._is_initialized:
            return {"error": "Gemini service not initialized"}

        # Ensure we have data to analyze
        if not metric_values or len(metric_values) < 2:
            return {"error": "Not enough data points for trend analysis"}

        # Format data for the model
        data_points = []
        for i, (ts, val) in enumerate(zip(timestamps, metric_values)):
            if i >= 30:  # Limit to most recent 30 points to avoid overwhelming the model
                break
            data_points.append({"timestamp": ts, "value": val})

        system_instruction = """
        You are a metrics analysis expert focusing on Kubernetes monitoring data.
        Analyze the metric trend to identify patterns, anomalies, and potential issues.
        Use statistical thinking to interpret the data accurately.
        """

        prompt = f"""
        # Kubernetes Metric Trend Analysis

        Analyze the trend in the '{metric_name}' metric using these data points:

        ```json
        {json.dumps(data_points, indent=2)}
        ```

        Calculate and provide:
        1. Overall trend direction (increasing, decreasing, stable, fluctuating)
        2. Growth/decline rate if applicable
        3. Volatility assessment
        4. Any anomalies or unusual patterns
        5. Potential implications for the Kubernetes system
        6. Whether this trend requires attention or action

        Return your analysis in JSON format.
        """

        try:
            response = await self.model.generate_content_async(
                model=self.model.name,
                contents=prompt,
                generation_config=self.generation_config,
                system_instruction=system_instruction,
                response_mime_type="application/json"
            )

            if hasattr(response, 'text'):
                try:
                    analysis = json.loads(response.text)
                    return analysis
                except json.JSONDecodeError:
                    # If not valid JSON, return as text
                    return {"analysis": response.text, "format": "text"}
            else:
                return {"error": "No response from model"}

        except Exception as e:
            logger.error(f"Error analyzing metrics trend: {e}")
            return {"error": f"Failed to analyze trend: {str(e)}"}

    async def get_deployment_recommendations(self,
                                           deployment_yaml: str,
                                           cluster_info: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Get recommendations to improve a Kubernetes deployment.

        Args:
            deployment_yaml: The YAML definition of the deployment
            cluster_info: Optional information about the cluster

        Returns:
            Dictionary with recommendations
        """
        if not self.model or not self._is_initialized:
            return {"error": "Gemini service not initialized"}

        system_instruction = """
        You are a Kubernetes deployment optimization expert with deep knowledge of:
        1. Resource allocation and quotas
        2. High availability configurations
        3. Security best practices
        4. Performance optimization
        5. Deployment strategies

        Analyze Kubernetes deployment YAML and provide specific, actionable recommendations
        to improve reliability, security, and performance. Focus on concrete changes that
        can be made to the YAML definition.
        """

        cluster_context = ""
        if cluster_info:
            cluster_context = f"""
            ## Cluster Context:
            ```json
            {json.dumps(cluster_info, indent=2)}
            ```
            """

        prompt = f"""
        # Kubernetes Deployment Review

        Analyze this Kubernetes deployment definition and provide recommendations:

        ```yaml
        {deployment_yaml}
        ```

        {cluster_context}

        Please provide:
        1. Security improvements
        2. Resource allocation recommendations
        3. High availability enhancements
        4. Performance optimizations
        5. Any missing best practices

        For each recommendation:
        - Explain WHY it's important
        - Show the EXACT changes to make (code snippets)
        - Rate the importance (Critical, High, Medium, Low)

        Return your recommendations in structured JSON format.
        """

        try:
            response = await self.model.generate_content_async(
                model=self.model.name,
                contents=prompt,
                generation_config=self.generation_config,
                system_instruction=system_instruction,
                response_mime_type="application/json"
            )

            if hasattr(response, 'text'):
                try:
                    recommendations = json.loads(response.text)
                    return recommendations
                except json.JSONDecodeError:
                    # If not valid JSON, return as text
                    return {"recommendations": response.text, "format": "text"}
            else:
                return {"error": "No response from model"}

        except Exception as e:
            logger.error(f"Error getting deployment recommendations: {e}")
            return {"error": f"Failed to analyze deployment: {str(e)}"}
