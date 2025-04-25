import asyncio
import json
import re
import uuid
import os
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Type, TypeVar, Union, AsyncIterator, Callable

import aiohttp
from google import genai
from google.genai import types
from loguru import logger
from pydantic import BaseModel, Field, ValidationError
from tenacity import (
    retry, 
    retry_if_exception_type, 
    stop_after_attempt, 
    wait_fixed, 
    wait_exponential,
    after_log
)

# Import RemediationAction from models
from app.models.k8s_models import RemediationAction
# Import custom exceptions
from app.core.exceptions import GeminiConnectionError, GeminiResponseError, ValidationError as AppValidationError
# Import k8s_executor to get command templates
from app.utils.k8s_executor import k8s_executor
# Import structured logger
from app.core import logger as app_logger
from app.core.logger import LogContext
# Import settings
import os
from app.core.config import settings

T = TypeVar("T")

# Define model types
class ModelType(str, Enum):
    PRO = "gemini-2.5-flash-preview-04-17"
    FLASH = "gemini-2.5-flash-preview-04-17"
    FLASH_LITE = "gemini-2.5-flash-preview-04-17"
    PRO_THINKING = "gemini-2.5-pro-preview"

# Define function calling modes
class FunctionCallingMode(str, Enum):
    AUTO = "AUTO"  # Model decides whether to call functions
    NONE = "NONE"  # Model doesn't call functions
    ANY = "ANY"    # Model can call any function
    MODE_UNSPECIFIED = "MODE_UNSPECIFIED"  # Default behavior

# --- Pydantic Models for Function Calling ---

class AnomalyAnalysis(BaseModel):
    """Model for anomaly analysis output."""

    root_cause: str = Field(..., description="Probable root cause of the anomaly")
    impact: str = Field(
        ..., description="Potential impact on the system or application"
    )
    severity: str = Field(
        ..., description="Estimated severity (e.g., High, Medium, Low)"
    )
    confidence: float = Field(
        ..., description="Confidence score (0.0-1.0) in the analysis"
    )
    recommendations: List[str] = Field(
        ..., description="Specific recommendations for investigation"
    )


class RemediationSteps(BaseModel):
    """Model for remediation steps output."""

    steps: List[str] = Field(
        ...,
        description="Suggested remediation command strings (e.g., 'operation_name param1=value param2=value')",
    )
    risks: List[str] = Field(
        ..., description="Potential risks associated with the steps"
    )
    expected_outcome: str = Field(
        ..., description="What should happen if the steps are successful"
    )
    confidence: float = Field(
        ..., description="Confidence score (0.0-1.0) in the suggested remediation"
    )


class MetricInsights(BaseModel):
    """Model for metric insights output."""

    patterns: List[str] = Field(..., description="Patterns observed in the metrics")
    anomalies: List[str] = Field(..., description="Potential anomalies detected")
    health_assessment: str = Field(..., description="Overall health assessment")
    recommendations: List[str] = Field(
        ..., description="Recommendations for improvement"
    )


# NEW Models for Query Generation and Verification
class GeneratedPromqlQueries(BaseModel):
    queries: List[str] = Field(
        ...,
        description="List of generated PromQL query strings suitable for monitoring common Kubernetes issues.",
    )
    reasoning: str = Field(
        ...,
        description="Explanation for why these queries were chosen based on the context.",
    )


class VerificationResult(BaseModel):
    success: bool = Field(
        ...,
        description="True if the remediation is assessed as successful, False otherwise.",
    )  # Changed name from resolved to success
    reasoning: str = Field(
        ...,
        description="Explanation for the success/failure assessment based on the provided context.",
    )  # Changed name from reason to reasoning
    confidence: float = Field(
        ..., description="Confidence score (0.0-1.0) in the verification assessment."
    )


# Add missing Prometheus models
class PrometheusQueryResponse(BaseModel):
    """Model for Prometheus query generation output."""

    query: str = Field(..., description="Generated Prometheus query string")
    explanation: str = Field(..., description="Explanation of how the query works")
    parameters: Optional[Dict[str, Any]] = Field(
        None, description="Parameters used in the query"
    )
    estimated_cardinality: Optional[str] = Field(
        None, description="Estimated cardinality/data points returned"
    )
    confidence: float = Field(
        ..., description="Confidence score (0.0-1.0) in the generated query"
    )


class QueryVerificationResponse(BaseModel):
    """Model for Prometheus query verification output."""

    is_valid: bool = Field(..., description="Whether the query is valid")
    issues: List[str] = Field(
        default_factory=list, description="List of issues found in the query"
    )
    suggested_improvements: List[str] = Field(
        default_factory=list, description="Suggested improvements for the query"
    )
    corrected_query: Optional[str] = Field(
        None, description="Corrected query if original had issues"
    )
    performance_assessment: str = Field(
        ..., description="Assessment of query performance"
    )


# NEW Models for Smart Remediation
class PrioritizedRemediation(BaseModel):
    """Model for prioritized remediation actions"""

    actions: List[RemediationAction] = Field(
        ..., description="Prioritized list of remediation actions"
    )
    reasoning: str = Field(..., description="Explanation for the prioritization")
    estimated_impact: Dict[str, str] = Field(
        ...,
        description="Estimated impact of each action (e.g., performance, availability)",
    )
    phased_approach: bool = Field(
        ..., description="Whether a phased/incremental approach is recommended"
    )
    phased_steps: Optional[List[str]] = Field(
        None, description="If phased, the recommended steps in sequence"
    )


class ResourceOptimizationSuggestion(BaseModel):
    """Model for resource optimization suggestions"""

    resource_type: str = Field(
        ..., description="Type of resource (e.g., deployment, statefulset)"
    )
    resource_name: str = Field(..., description="Name of the resource")
    namespace: str = Field(..., description="Namespace of the resource")
    current_cpu_request: Optional[str] = Field(None, description="Current CPU request")
    current_cpu_limit: Optional[str] = Field(None, description="Current CPU limit")
    current_memory_request: Optional[str] = Field(
        None, description="Current memory request"
    )
    current_memory_limit: Optional[str] = Field(None, description="Current memory limit")
    suggested_cpu_request: Optional[str] = Field(
        None, description="Suggested CPU request"
    )
    suggested_cpu_limit: Optional[str] = Field(None, description="Suggested CPU limit")
    suggested_memory_request: Optional[str] = Field(
        None, description="Suggested memory request"
    )
    suggested_memory_limit: Optional[str] = Field(
        None, description="Suggested memory limit"
    )
    reasoning: str = Field(..., description="Reasoning behind the suggestion")
    confidence: float = Field(..., description="Confidence in the suggestion (0.0-1.0)")


class RootCauseAnalysisReport(BaseModel):
    """Model for detailed RCA reports"""

    title: str = Field(..., description="Title summarizing the issue")
    summary: str = Field(..., description="Executive summary of the incident")
    root_cause: str = Field(..., description="Identified root cause")
    detection_details: Dict[str, Any] = Field(
        ..., description="Details of how the issue was detected"
    )
    timeline: List[Dict[str, str]] = Field(..., description="Timeline of the incident")
    resolution_steps: List[str] = Field(
        ..., description="Steps taken to resolve the issue"
    )
    metrics_analysis: str = Field(..., description="Analysis of relevant metrics")
    recommendations: List[str] = Field(
        ..., description="Recommendations to prevent recurrence"
    )
    related_resources: List[str] = Field(
        ..., description="Related Kubernetes resources"
    )


class MetricTrendAnalysis(BaseModel):
    """Model for metric trend analysis output."""

    overall_trend: str = Field(..., description="Overall trend of the metric")
    rate_of_change: str = Field(..., description="Rate of change of the metric")
    significant_patterns: List[str] = Field(
        ..., description="Significant patterns observed in the metric"
    )
    implications: str = Field(
        ..., description="Potential implications for the system health"
    )
    recommendation: str = Field(..., description="Recommendation based on the analysis")


class ChatResponse(BaseModel):
    """Model for chat responses."""

    response: str = Field(..., description="The chat response text")
    confidence: float = Field(0.9, description="Confidence in the response (0.0-1.0)")
    suggested_actions: Optional[List[str]] = Field(
        None, description="Suggested actions the user might want to take"
    )
    referenced_resources: Optional[List[str]] = Field(
        None, description="Kubernetes resources referenced in the response"
    )


# --- Function Declaration Schema Types ---
class SchemaType(str, Enum):
    """Type of schema for function parameters."""
    OBJECT = "OBJECT"
    STRING = "STRING"
    NUMBER = "NUMBER"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    ARRAY = "ARRAY"


class FunctionDeclarationSchemaProperty(BaseModel):
    """
    Model for function schema property definition.
    """
    type: SchemaType
    description: Optional[str] = None
    items: Optional[Dict[str, Any]] = None  # Add items field for array properties


class FunctionDeclarationSchema(BaseModel):
    """
    Model for function declaration schema.
    """
    type: SchemaType = SchemaType.OBJECT
    properties: Dict[str, FunctionDeclarationSchemaProperty]
    required: Optional[List[str]] = None
    description: Optional[str] = None


class FunctionDeclaration(BaseModel):
    """
    Model for function declaration.
    """
    name: str
    description: Optional[str] = None
    parameters: Optional[FunctionDeclarationSchema] = None


class FunctionCall(BaseModel):
    """
    Model for function call from Gemini.
    """
    name: str
    args: Dict[str, Any]


class FunctionResponse(BaseModel):
    """
    Model for function response to Gemini.
    """
    name: str
    response: Dict[str, Any]


class GeminiService:
    """
    Service to interact with Google's Gemini AI using async patterns, proper error handling,
    and enhanced function calling capabilities.
    """

    def __init__(self, api_key: Optional[str] = None, model_type: Union[ModelType, str] = ModelType.PRO):
        """
        Initialize the Gemini service with the new Gen AI SDK client.
        
        Args:
            api_key: Optional API key override. If not provided, will attempt to use the one from environment.
            model_type: The model type to use. Can be ModelType enum or string. Defaults to PRO.
        """
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or settings.gemini_api_key
        
        # Handle both ModelType enum and string inputs
        self.model_type = model_type.value if isinstance(model_type, ModelType) else model_type
        self.client = None
        
        # Initialize client if API key is available
        if self.api_key:
            try:
                # Initialize the client with the API key
                self.client = genai.Client(api_key=self.api_key)
                logger.info(f"Initialized Gemini client for model: {self.model_type}")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
                # We'll raise exceptions only when actual API calls are made
        else:
            logger.warning("No API key provided for Gemini service")
        
        # Track function definitions for different use cases
        self._function_declarations = {}
        
        # Initialize function registries
        self._register_functions()

        # Initialize chat_sessions dictionary
        self.chat_sessions = {}
        self._session_lock = asyncio.Lock()
        self._http_session = None

        # Define standard system instructions
        self.system_instructions = {
            "k8s_expert": """You are KubeWise AI, an expert in Kubernetes troubleshooting and remediation. 
Your role is to analyze metrics, logs, and other data to identify issues in Kubernetes clusters and suggest remediation steps.
Provide clear, detailed explanations and always consider the potential risks of any remediation actions.
When recommending commands, use the standard kubectl syntax and include proper namespaces.""",
            
            "prompt_engineer": """You are a PromQL Expert specializing in crafting efficient and effective Prometheus queries.
Your responses should be concise, precise, and focused on the optimal query patterns.
Consider query performance, cardinality, and accuracy in your recommendations.""",
            
            "remediation_agent": """You are the KubeWise Remediation Agent, responsible for analyzing Kubernetes issues and 
suggesting appropriate remediation steps. Use your knowledge of Kubernetes to prioritize fixes that minimize risk 
and service disruption. Always explain the rationale behind your recommendations and any potential side effects.""",
        }

        logger.info(f"GeminiService initialized with model: {self.model_type}")
        
    def _register_functions(self):
        """Register function declarations for different scenarios."""
        
        # Analyze Anomaly function
        self._function_declarations["analyze_anomaly"] = [
            FunctionDeclaration(
                name="analyze_kubernetes_anomaly",
                description="Analyzes a Kubernetes anomaly and provides insights on root cause and impact",
                parameters=FunctionDeclarationSchema(
                    properties={
                        "root_cause": FunctionDeclarationSchemaProperty(
                            type=SchemaType.STRING,
                            description="Probable root cause of the anomaly"
                        ),
                        "impact": FunctionDeclarationSchemaProperty(
                            type=SchemaType.STRING,
                            description="Potential impact on the system or application"
                        ),
                        "severity": FunctionDeclarationSchemaProperty(
                            type=SchemaType.STRING,
                            description="Estimated severity (High, Medium, or Low)"
                        ),
                        "confidence": FunctionDeclarationSchemaProperty(
                            type=SchemaType.NUMBER,
                            description="Confidence score (0.0-1.0) in the analysis"
                        ),
                        "recommendations": FunctionDeclarationSchemaProperty(
                            type=SchemaType.ARRAY,
                            description="Specific recommendations for investigation",
                            items={"type": "STRING"}  # Add items field
                        )
                    },
                    required=["root_cause", "impact", "severity", "confidence", "recommendations"]
                )
            )
        ]
        
        # Suggest Remediation function
        self._function_declarations["suggest_remediation"] = [
            FunctionDeclaration(
                name="suggest_kubernetes_remediation",
                description="Suggests remediation steps for a Kubernetes issue",
                parameters=FunctionDeclarationSchema(
                    properties={
                        "steps": FunctionDeclarationSchemaProperty(
                            type=SchemaType.ARRAY,
                            description="Suggested remediation command strings",
                            items={"type": "STRING"}  # Add items field
                        ),
                        "risks": FunctionDeclarationSchemaProperty(
                            type=SchemaType.ARRAY,
                            description="Potential risks associated with the steps",
                            items={"type": "STRING"}  # Add items field
                        ),
                        "expected_outcome": FunctionDeclarationSchemaProperty(
                            type=SchemaType.STRING,
                            description="What should happen if the steps are successful"
                        ),
                        "confidence": FunctionDeclarationSchemaProperty(
                            type=SchemaType.NUMBER,
                            description="Confidence score (0.0-1.0) in the suggested remediation"
                        )
                    },
                    required=["steps", "risks", "expected_outcome", "confidence"]
                )
            )
        ]
        
        # Verify Remediation function
        self._function_declarations["verify_remediation"] = [
            FunctionDeclaration(
                name="verify_kubernetes_remediation",
                description="Verifies if a remediation action was successful",
                parameters=FunctionDeclarationSchema(
                    properties={
                        "success": FunctionDeclarationSchemaProperty(
                            type=SchemaType.BOOLEAN,
                            description="True if remediation was successful, False otherwise"
                        ),
                        "reasoning": FunctionDeclarationSchemaProperty(
                            type=SchemaType.STRING,
                            description="Explanation for the success/failure assessment"
                        ),
                        "confidence": FunctionDeclarationSchemaProperty(
                            type=SchemaType.NUMBER,
                            description="Confidence score (0.0-1.0) in the verification assessment"
                        )
                    },
                    required=["success", "reasoning", "confidence"]
                )
            )
        ]
        
        # Generate PromQL Queries function
        self._function_declarations["generate_promql"] = [
            FunctionDeclaration(
                name="generate_promql_queries",
                description="Generates PromQL queries for monitoring Kubernetes resources",
                parameters=FunctionDeclarationSchema(
                    properties={
                        "queries": FunctionDeclarationSchemaProperty(
                            type=SchemaType.ARRAY,
                            description="List of generated PromQL query strings",
                            items={"type": "STRING"}  # Add items field
                        ),
                        "reasoning": FunctionDeclarationSchemaProperty(
                            type=SchemaType.STRING,
                            description="Explanation for why these queries were chosen"
                        )
                    },
                    required=["queries", "reasoning"]
                )
            )
        ]
    
    async def __aenter__(self) -> "GeminiService":
        """
        Async context manager entry.

        Returns:
            Self instance with initialized HTTP session
        """
        # Initialize HTTP session for async operations if needed
        if not self._http_session:
            self._http_session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        Async context manager exit.
        """
        # Close HTTP session if it exists
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    async def close(self) -> None:
        """
        Close any open resources.
        """
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    def is_api_key_missing(self) -> bool:
        """Check if API key is missing or client initialization failed."""
        return not self.api_key or not self.client
        
    def _convert_schema_to_new_format(self, function_declarations: List[FunctionDeclaration]) -> List[types.FunctionDeclaration]:
        """
        Convert the old schema format to the new FunctionDeclaration format.
        
        Args:
            function_declarations: List of function declarations in the old format
            
        Returns:
            List of function declarations in the new format
        """
        new_declarations = []
        for func in function_declarations:
            # Handle both dict and Pydantic model formats
            if isinstance(func, dict):
                name = func.get("name", "")
                description = func.get("description", "")
                parameters = func.get("parameters", {})
                param_type = parameters.get("type", "object")
                properties = parameters.get("properties", {})
                required = parameters.get("required", [])
            else:
                # It's a Pydantic model
                name = func.name
                description = func.description or ""
                parameters = func.parameters.dict() if func.parameters else {}
                param_type = parameters.get("type", "object")
                properties = parameters.get("properties", {})
                required = parameters.get("required", [])
            
            # Convert properties to the expected format
            converted_properties = {}
            for k, v in properties.items():
                if isinstance(v, dict):
                    converted_properties[k] = types.Schema(**v)
                else:
                    # If it's a Pydantic model, convert to dict first
                    prop_dict = v.dict() if hasattr(v, "dict") else {"type": v.type.value, "description": v.description}
                    converted_properties[k] = types.Schema(**prop_dict)
            
            new_declaration = types.FunctionDeclaration(
                name=name,
                description=description,
                parameters=types.Schema(
                    type=param_type,
                    properties=converted_properties,
                    required=required
                )
            )
            new_declarations.append(new_declaration)
        return new_declarations

    async def _call_gemini_with_functions(
        self,
        prompt: str,
        function_declarations: List[FunctionDeclaration],
        function_calling_config: Dict[str, Any] = None,
        system_instruction: Optional[str] = None,
        model_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Call Gemini with function calling enabled using the new Google Gen AI SDK.
        
        Args:
            prompt: The prompt to send to Gemini
            function_declarations: List of function declarations in JSON format
            function_calling_config: Configuration for function calling
            system_instruction: Optional system instruction to prepend
            model_type: Optional model type override
            
        Returns:
            Dictionary with function call results
            
        Raises:
            GeminiConnectionError: When there's an issue with the connection
            GeminiResponseError: When there's an issue with the response
        """
        if self.is_api_key_missing():
            error_msg = "Gemini service not initialized"
            logger.error(error_msg)
            raise GeminiConnectionError(error_msg)
        
        try:
            # Use provided model type or default to the initialized one
            model = model_type or self.model_type
            
            # Configure generation settings
            generation_config = types.GenerateContentConfig(
                temperature=0.2,
                top_p=0.95,
                top_k=30,
                max_output_tokens=2048,
            )
            
            # Convert function declarations to the new format
            tools = []
            for func in function_declarations:
                # Handle both dict and Pydantic model formats
                if isinstance(func, dict):
                    name = func.get("name", "")
                    description = func.get("description", "")
                    parameters = func.get("parameters", {})
                else:
                    # It's a Pydantic model
                    name = func.name
                    description = func.description or ""
                    parameters = func.parameters.dict() if func.parameters else {}
                
                # Create a function declaration in the new format
                tools.append({
                    "function_declarations": [
                        {
                            "name": name,
                            "description": description,
                            "parameters": parameters
                        }
                    ]
                })
            
            # Configure automatic function calling
            automatic_function_calling = None
            if function_calling_config:
                mode = function_calling_config.get("mode", "ANY")
                if mode == "NONE":
                    automatic_function_calling = {"disable": True}
                # For ANY/AUTO modes, we don't need to set anything as it's the default
            
            # Build the content parts
            content_parts = []
            
            # Add system instruction if provided
            if system_instruction:
                content_parts.append({
                    "role": "model",  # Using model role which is valid
                    "parts": [{"text": system_instruction}]
                })
            
            # Add user prompt
            content_parts.append({
                "role": "user",
                "parts": [{"text": prompt}]
            })
            
            # Generate content with the client API
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=model,
                contents=content_parts,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    top_p=0.95,
                    top_k=30,
                    max_output_tokens=2048,
                    tools=tools,
                    automatic_function_calling=automatic_function_calling
                )
            )
            
            # Extract function calls from the response
            result = {"content": response.text}
            
            # Check for function calls in the new SDK format with proper null checks
            if hasattr(response, "candidates") and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, "content") and candidate.content:
                    content = candidate.content
                    
                    if hasattr(content, "parts") and content.parts:
                        for part in content.parts:
                            # Check for function calls in the part with proper null checks
                            if hasattr(part, "function_call") and part.function_call is not None:
                                function_call = part.function_call
                                # Make sure function_call has the name attribute and it's not None
                                if hasattr(function_call, "name") and function_call.name is not None:
                                    args = {}
                                    if hasattr(function_call, "args") and function_call.args is not None:
                                        try:
                                            # First check if args is already a dict (no need to parse)
                                            if isinstance(function_call.args, dict):
                                                args = function_call.args
                                            else:
                                                args = json.loads(function_call.args)
                                        except json.JSONDecodeError:
                                            logger.warning(f"Failed to parse function call args: {function_call.args}")
                                    
                                    result["function_call"] = {
                                        "name": function_call.name,
                                        "args": args
                                    }
                                    break
            
            return result
        
        except Exception as e:
            error_msg = f"Error calling Gemini API: {str(e)}"
            logger.error(error_msg)
            raise GeminiResponseError(error_msg)

    @retry(
        retry=retry_if_exception_type((ConnectionError, TimeoutError, aiohttp.ClientError, GeminiConnectionError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        after=after_log(logger, "WARNING")
    )
    @app_logger.log_context(component="gemini")
    async def _call_gemini_with_model(
        self,
        prompt: str,
        output_model: Type[BaseModel],
        model_type: Optional[Union[ModelType, str]] = None,
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> dict:
        """
        Call Gemini API and parse the response into a structured model using the new Gen AI SDK.

        Args:
            prompt: Prompt to send to Gemini
            output_model: Pydantic model class to parse the response into
            model_type: Optional model type to use for this request (ModelType enum or string)
            system_instruction: Optional system instruction
            **kwargs: Additional arguments to pass to the model

        Returns:
            Dictionary representation of the parsed model

        Raises:
            GeminiConnectionError: When unable to connect to the Gemini API
        """
        # Special handling for function calling capabilities
        if hasattr(output_model, "__annotations__") and output_model.__name__ in self._function_declarations:
            # Check if we have function declarations for this model
            function_declarations = self._function_declarations.get(output_model.__name__)
            if function_declarations:
                try:
                    # Use function calling implementation
                    result = await self._call_gemini_with_functions(
                        prompt=prompt,
                        function_declarations=function_declarations,
                        system_instruction=system_instruction,
                        model_type=model_type
                    )
                    
                    # Convert function call results to the expected output model
                    if result.get("function_call") and result["function_call"].get("args"):
                        return result["function_call"]["args"]
                    else:
                        # Fallback to traditional extraction if no function call results
                        return {"text": result.get("content", "")}
                except Exception as e:
                    logger.warning(f"Function calling attempt failed, falling back to traditional method: {e}")
                    # Fallback to traditional implementation
        
        # Traditional implementation (if function calling fails or not applicable)
        if self.is_api_key_missing():
            error_msg = "API key is missing for Gemini service"
            logger.error(error_msg)
            raise GeminiConnectionError(error_msg)

        # Use provided model type or default
        model = model_type.value if isinstance(model_type, ModelType) else model_type or self.model_type

        # Define baseline error details for potential exceptions
        error_details = {
            "model": model,
            "expected_output_model": output_model.__name__,
        }

        try:
            # Add the model class to the prompt to help with structured output
            model_schema = self._get_model_schema(output_model)
            structured_prompt = f"{prompt}\n\nPlease respond with a valid JSON object following this structure:\n{json.dumps(model_schema, indent=2)}"
            
            # Create content parts
            content_parts = []
            
            # Add system instruction if provided
            if system_instruction:
                content_parts.append({
                    "role": "model",  # Changed from "system" to "model" which is a valid role
                    "parts": [{"text": system_instruction}]
                })
            
            # Add user prompt
            content_parts.append({
                "role": "user",
                "parts": [{"text": structured_prompt}]
            })
            
            try:
                # Generate content with the client
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=model,
                    contents=content_parts,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        top_p=0.95,
                        top_k=40,
                        max_output_tokens=2048,
                    )
                )
                
                # Get response text
                response_text = response.text if hasattr(response, "text") else ""
                
            except Exception as api_error:
                logger.error(f"Error generating content: {api_error}")
                raise GeminiResponseError(f"Failed to generate content: {api_error}")
            
            try:
                parsed_data = await self._extract_json_from_text(response_text, output_model)
                if parsed_data:
                    # Validate the data against the output model
                    try:
                        output_model(**parsed_data)
                        return parsed_data
                    except ValidationError as ve:
                        # Try to fix validation errors
                        fixed_data = await self._fix_validation_errors(parsed_data, output_model, ve)
                        return fixed_data
                else:
                    error_msg = "Could not extract JSON from Gemini response"
                    logger.error(f"{error_msg}. Response: {response_text[:200]}...")
                    minimal_response = await self._create_minimal_response(output_model)
                    return minimal_response
            except json.JSONDecodeError as je:
                error_msg = f"JSON parsing error: {je}"
                logger.error(f"{error_msg}. Response: {response_text[:200]}...")
                minimal_response = await self._create_minimal_response(output_model)
                return minimal_response

        except Exception as e:
            error_msg = f"Gemini API error: {str(e)}"
            error_details["error"] = str(e)
            logger.error(error_msg, exc_info=True)
            
            if "Safety" in str(e):
                error_msg = f"Gemini safety error: {str(e)}"
                raise GeminiResponseError(error_msg, error_details)
            elif "Rate" in str(e) or "quota" in str(e).lower():
                error_msg = f"Gemini rate limit or quota exceeded: {str(e)}"
                raise GeminiConnectionError(error_msg, error_details)
            elif "Invalid" in str(e):
                error_msg = f"Gemini invalid request: {str(e)}"
                raise GeminiResponseError(error_msg, error_details)
            else:
                error_msg = f"Gemini API error: {str(e)}"
                raise GeminiConnectionError(error_msg, error_details)

    def _get_model_schema(self, model_class: Type[BaseModel]) -> Dict[str, Any]:
        """
        Generate a simplified schema representation of a Pydantic model.
        
        Args:
            model_class: The Pydantic model class
            
        Returns:
            Dictionary representing the schema
        """
        schema = {}
        
        # Get model fields from __annotations__ or __fields__
        fields = getattr(model_class, "__annotations__", {})
        
        if not fields and hasattr(model_class, "__fields__"):
            fields = {field_name: field.annotation for field_name, field in model_class.__fields__.items()}
        
        for field_name, field_type in fields.items():
            # Get the field from the model's fields
            field_info = None
            if hasattr(model_class, "__fields__"):
                field_info = model_class.__fields__.get(field_name)
            
            # Determine the field's type and create example value
            if field_info and hasattr(field_info, "field_info") and field_info.field_info and field_info.field_info.description:
                description = field_info.field_info.description
            else:
                description = f"{field_name} field"
            
            # Check if it's a List type
            if hasattr(field_type, "__origin__") and field_type.__origin__ is list:
                # For List types, get the inner type
                if hasattr(field_type, "__args__") and field_type.__args__:
                    inner_type = field_type.__args__[0]
                    
                    # Check if inner type is another Pydantic model
                    if hasattr(inner_type, "__annotations__"):
                        schema[field_name] = [self._get_model_schema(inner_type)]
                    else:
                        # Default example for simple types
                        schema[field_name] = [f"Example {field_name} item with: {description}"]
                else:
                    schema[field_name] = [f"Example {field_name} item"]
            
            # Check if it's an Optional type
            elif hasattr(field_type, "__origin__") and field_type.__origin__ is Union:
                # For Optional types, get the non-None type
                inner_types = [t for t in field_type.__args__ if t is not type(None)]
                if inner_types:
                    inner_type = inner_types[0]
                    
                    # Check if inner type is another Pydantic model
                    if hasattr(inner_type, "__annotations__"):
                        schema[field_name] = self._get_model_schema(inner_type)
                    elif inner_type is dict or (hasattr(inner_type, "__origin__") and inner_type.__origin__ is dict):
                        schema[field_name] = {"key": f"Example value with: {description}"}
                    else:
                        # Default example for simple types
                        schema[field_name] = f"Example {field_name} with: {description}"
                else:
                    schema[field_name] = None
            
            # Check if it's a Dict type
            elif hasattr(field_type, "__origin__") and field_type.__origin__ is dict:
                schema[field_name] = {"key": f"Example value with: {description}"}
            
            # Check if it's another Pydantic model
            elif hasattr(field_type, "__annotations__"):
                schema[field_name] = self._get_model_schema(field_type)
            
            # Default for simple types
            else:
                if field_type is bool:
                    schema[field_name] = True
                elif field_type is int or field_type is float:
                    schema[field_name] = 0.9 if field_name == "confidence" else 1
                else:
                    schema[field_name] = f"Example {field_name} with: {description}"
        
        return schema

    @app_logger.log_context(component="gemini")
    async def _fix_validation_errors(self, data: dict, output_model: Type[BaseModel], validation_error: ValidationError) -> dict:
        """
        Attempt to fix common validation errors in function call responses.

        Args:
            data: The original data from the function call
            output_model: The target Pydantic model
            validation_error: The validation error that occurred

        Returns:
            Dict with fixed data that should pass validation
        """
        fixed_data = data.copy()

        # Extract error information
        for error in validation_error.errors():
            field = error["loc"][0] if error["loc"] else None
            error_type = error["type"]

            if field:
                app_logger.debug(
                    f"Attempting to fix validation error",
                    field=field,
                    error_type=error_type
                )
                
                # Common fixes based on error type
                if error_type == "missing":
                    # Field is missing, set a default value based on field type
                    field_info = output_model.model_fields.get(field)
                    if field_info:
                        if field_info.annotation == str:
                            fixed_data[field] = ""
                        elif field_info.annotation == int:
                            fixed_data[field] = 0
                        elif field_info.annotation == float:
                            fixed_data[field] = 0.0
                        elif field_info.annotation == bool:
                            fixed_data[field] = False
                        elif field_info.annotation == List[str]:
                            fixed_data[field] = []
                        elif field_info.annotation == Dict[str, Any]:
                            fixed_data[field] = {}
                
                elif error_type == "type_error.integer":
                    # Expected integer but got string, try to convert
                    if field in fixed_data and isinstance(fixed_data[field], str):
                        try:
                            fixed_data[field] = int(fixed_data[field])
                        except ValueError:
                            fixed_data[field] = 0
                
                elif error_type == "type_error.float":
                    # Expected float but got string, try to convert
                    if field in fixed_data and isinstance(fixed_data[field], str):
                        try:
                            fixed_data[field] = float(fixed_data[field])
                        except ValueError:
                            fixed_data[field] = 0.0
                
                elif error_type == "type_error.list":
                    # Expected list but got something else
                    if field in fixed_data and isinstance(fixed_data[field], str):
                        # Convert single string to list with one item
                        fixed_data[field] = [fixed_data[field]]
                    else:
                        fixed_data[field] = []
                
                elif "value_error" in error_type and "confidence" in field:
                    # Common issue with confidence values out of range
                    if field in fixed_data:
                        if isinstance(fixed_data[field], (int, float)):
                            if fixed_data[field] > 1.0:
                                fixed_data[field] = 1.0
                            elif fixed_data[field] < 0.0:
                                fixed_data[field] = 0.0

        # Try to validate the fixed data
        try:
            validated = output_model(**fixed_data)
            app_logger.debug("Validation errors fixed successfully")
            return validated.model_dump() if hasattr(validated, "model_dump") else validated.dict()
        except ValidationError:
            app_logger.warning(
                "Failed to fix validation errors, creating minimal response"
            )
            # If still failing, use minimal valid response
            return await self._create_minimal_response(output_model)

    @app_logger.log_context(component="gemini")
    async def _extract_json_from_text(
        self, text: str, output_model: Type[BaseModel]
    ) -> Optional[Dict[str, Any]]:
        """
        Extract and validate JSON from text response.
        
        Args:
            text: Text response from Gemini
            output_model: The expected Pydantic model class
            
        Returns:
            Extracted JSON data that conforms to the model, or None if extraction fails
        """
        try:
            # If the text is a simple error message or doesn't contain JSON-like structures,
            # create a minimal valid response
            if not any(char in text for char in "{[]}"):
                app_logger.debug("No JSON-like structures in response")
                return await self._create_minimal_response(output_model)

            # Pattern to match JSON within triple backticks, markdown code blocks, or standalone
            json_pattern = (
                r"```(?:json)?\s*([\s\S]*?)```|`([\s\S]*?)`|([\{\[][\s\S]*?[\}\]])"
            )
            matches = re.findall(json_pattern, text)

            # Try different extraction methods
            json_candidates = []

            # Process matches from regex
            for match in matches:
                for group in match:
                    if group and (group.strip().startswith("{") or group.strip().startswith("[")):
                        json_candidates.append(group.strip())

            # If no matches, try the whole text if it looks like JSON
            if not json_candidates and (
                text.strip().startswith("{") or text.strip().startswith("[")
            ):
                json_candidates.append(text.strip())

            # Try each candidate
            for json_str in json_candidates:
                try:
                    json_data = json.loads(json_str)
                    app_logger.debug("Successfully extracted JSON from text response")
                    return json_data
                except json.JSONDecodeError:
                    continue

            app_logger.warning("Could not extract valid JSON from text response")
            # Return a minimal valid response instead of None
            return await self._create_minimal_response(output_model)
        except Exception as e:
            app_logger.error(
                "Error extracting JSON from text",
                exception=e
            )
            # Return a minimal valid response instead of None
            return await self._create_minimal_response(output_model)

    @app_logger.log_context(component="gemini")
    async def _create_minimal_response(self, model_class: Type[T]) -> Dict[str, Any]:
        """Create a minimal valid response for the given Pydantic model class."""
        try:
            defaults = {}

            # Get model fields
            for name, field in model_class.model_fields.items():
                # Create minimal default values based on field type
                if field.annotation == str:
                    defaults[name] = field.default if field.default is not None else "Not available"
                elif field.annotation == int:
                    defaults[name] = field.default if field.default is not None else 0
                elif field.annotation == float:
                    defaults[name] = field.default if field.default is not None else 0.0
                elif field.annotation == bool:
                    defaults[name] = field.default if field.default is not None else False
                elif field.annotation == List[str]:
                    defaults[name] = field.default if field.default is not None else ["No data available"]
                elif field.annotation == Dict[str, Any]:
                    defaults[name] = field.default if field.default is not None else {}
                elif field.annotation is None:
                    # If annotation is not specified, try to infer from default
                    if field.default is not None:
                        defaults[name] = field.default
                    else:
                        defaults[name] = "Not available"
                else:
                    # For complex types, use empty instances
                    try:
                        defaults[name] = None
                    except Exception:
                        defaults[name] = "Not available"

            instance = model_class(**defaults)
            app_logger.debug(
                f"Created minimal response for {model_class.__name__}"
            )
            return instance.model_dump() if hasattr(instance, "model_dump") else instance.dict()

        except Exception as e:
            app_logger.error(
                f"Error creating minimal response for {model_class.__name__}",
                exception=e
            )
            # Last resort - try with empty dict and accept the error
            try:
                instance = model_class()
                return instance.model_dump() if hasattr(instance, "model_dump") else instance.dict()
            except Exception as e2:
                app_logger.error(
                    f"Failed to create instance with default init for {model_class.__name__}",
                    exception=e2
                )
                return {"error": f"Failed to create valid {model_class.__name__} instance"}

    @app_logger.log_context(component="gemini")
    async def _safely_handle_response(
        self, 
        response: Any, 
        output_model: Optional[Type[BaseModel]] = None, 
        default_return: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Safely handle responses from Gemini API and check for errors in a consistent way.

        This centralizes error handling logic to prevent KeyError issues when checking for errors.

        Args:
            response: The response from Gemini API (could be None, dict, BaseModel, or string)
            output_model: The expected Pydantic model class (optional)
            default_return: What to return if response is invalid (defaults to minimal response of output_model)

        Returns:
            The original response if valid, or a standardized error response
        """
        # Set up default return value if not provided
        if default_return is None and output_model is not None:
            default_return = await self._create_minimal_response(output_model)
        elif default_return is None:
            default_return = {"error": "Invalid response", "details": "No data returned"}

        # Handle None responses
        if response is None:
            app_logger.warning("Received None response from Gemini API")
            return default_return

        # Handle string responses
        if isinstance(response, str):
            app_logger.warning("Received string response instead of structured data")
            if output_model:
                # Try to extract JSON from text
                json_data = await self._extract_json_from_text(response, output_model)
                if json_data:
                    return json_data
            return {"text": response}

        # Handle BaseModel responses - convert to dict
        if isinstance(response, BaseModel):
            try:
                return response.model_dump() if hasattr(response, "model_dump") else response.dict()
            except Exception as e:
                app_logger.error(
                    "Error converting BaseModel to dict",
                    exception=e
                )
                return default_return

        # Now handle dict responses and check for error conditions
        if isinstance(response, dict):
            # Check for explicit error key
            if "error" in response:
                error_msg = response["error"]
                app_logger.warning(
                    "Error in Gemini response",
                    error=error_msg
                )
                # If the error response is already well-formed, return it as is
                if "details" in response:
                    return response
                # Otherwise, create a well-formed error response
                return {"error": error_msg, "details": response.get("details", {})}

            # Check for other error indicators
            if response.get("status") == "failed":
                app_logger.warning(
                    "Failed status in Gemini response",
                    reason=response.get("reason", "Unknown reason")
                )
                return {"error": "Operation failed", "details": response}

            # No error found, return the response
            return response

        # If we get here, the response is an unhandled type
        app_logger.warning(f"Unhandled response type: {type(response)}")
        return default_return

    # --- Chat Methods ---
    @retry(
        retry=retry_if_exception_type(GeminiConnectionError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        after=after_log(logger, "WARNING")
    )
    @app_logger.log_context(component="gemini", operation="create_chat_session")
    async def create_chat_session(
        self, system_instruction: Optional[str] = None, history_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Create a new chat session with Gemini.

        Args:
            system_instruction: Optional system instructions to include
            history_id: Optional history ID to use (will generate one if not provided)

        Returns:
            The history ID for this chat session or None on failure
            
        Raises:
            GeminiConnectionError: When unable to connect to the API
            GeminiResponseError: When there's an issue with the response
        """
        if not self.client or not self.api_key:
            error_msg = "Gemini service not initialized"
            app_logger.error(error_msg)
            raise GeminiConnectionError(error_msg)

        # Generate a random ID if none provided
        if not history_id:
            history_id = str(uuid.uuid4())

        try:
            async with self._session_lock:
                # Create a new chat session with the given history ID
                self.chat_sessions[history_id] = {
                    "created_at": datetime.utcnow(),
                    "messages": [],
                    "system_instruction": system_instruction
                }
                
                app_logger.info(
                    "Chat session created",
                    history_id=history_id,
                    has_system_instruction=system_instruction is not None
                )
                
                return history_id

        except Exception as e:
            app_logger.error(
                "Error creating chat session",
                exception=e,
                history_id=history_id
            )
            
            error_msg = f"Failed to create chat session: {str(e)}"
            raise GeminiResponseError(error_msg)

    @retry(
        retry=retry_if_exception_type(GeminiConnectionError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        after=after_log(logger, "WARNING")
    )
    @app_logger.log_context(component="gemini", operation="chat_message")
    async def chat_message(
        self,
        session_id: str,
        message: str,
        chat_history: List[Dict[str, Any]],
        context_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Process a user chat message and generate a response.
        
        Args:
            session_id: The ID of the chat session
            message: The user message to process
            chat_history: List of previous chat messages
            context_data: Optional context data to include in the prompt
            
        Returns:
            Dictionary containing the chat response
            
        Raises:
            GeminiConnectionError: When unable to connect to the API
            GeminiResponseError: When there's an issue with the response
        """
        app_logger.debug(
            "Processing chat message",
            session_id=session_id,
            message_length=len(message)
        )

        if not self.client or not self.api_key:
            error_msg = "Gemini service not initialized"
            app_logger.error(error_msg)
            raise GeminiConnectionError(error_msg)

        # Format the chat history
        formatted_history = []
        for entry in chat_history:
            role = entry.get("role", "unknown")
            content = entry.get("content", "")
            formatted_history.append(f"{role.capitalize()}: {content}")

        # Format context data if available
        context_str = ""
        if context_data:
            context_str = json.dumps(context_data, indent=2, default=str)

        prompt = f"""
        # Kubernetes Assistant Chat Session

        ## Chat History:
        {chr(10).join(formatted_history) if formatted_history else "This is a new conversation."}

        ## Available Context Information:
        {context_str if context_str else "No additional context information available."}

        ## Current User Message:
        {message}

        ## Task:
        Respond to the user message in a helpful, informative, and concise manner.
        When referring to specific Kubernetes resources or metrics, cite the relevant information
        from the context if available.
        If asked about specific recommendations, provide practical advice based on Kubernetes best practices.
        If you don't have enough information, clearly state what additional details would be helpful.

        Provide your response using the 'ChatResponse' Pydantic model.
        """

        app_logger.debug(
            "Calling Gemini for chat response",
            session_id=session_id
        )
        
        try:
            # Get system instruction from chat session if available
            system_instruction = None
            async with self._session_lock:
                if session_id in self.chat_sessions:
                    system_instruction = self.chat_sessions[session_id].get("system_instruction")
            
            # Call Gemini with the prompt
            response = await self._call_gemini_with_model(
                prompt=prompt,
                output_model=ChatResponse,
                system_instruction=system_instruction
            )
            
            # Store the message in chat history
            async with self._session_lock:
                if session_id in self.chat_sessions:
                    self.chat_sessions[session_id]["messages"].append({
                        "role": "user",
                        "content": message,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    
                    # Store the response
                    self.chat_sessions[session_id]["messages"].append({
                        "role": "assistant",
                        "content": response.get("response", ""),
                        "timestamp": datetime.utcnow().isoformat()
                    })
            
            app_logger.debug(
                "Successfully processed chat message",
                session_id=session_id,
                response_length=len(response.get("response", ""))
            )
            
            return response
            
        except Exception as e:
            app_logger.error(
                "Error processing chat message",
                exception=e,
                session_id=session_id
            )
            
            error_msg = f"Failed to process chat message: {str(e)}"
            
            if isinstance(e, GeminiConnectionError):
                raise
            else:
                raise GeminiResponseError(error_msg)

    @app_logger.log_context(component="gemini")
    async def get_chat_history(self, history_id: str) -> List[Dict[str, Any]]:
        """Get the history of a chat session."""
        if (history_id not in self.chat_sessions):
            app_logger.warning(
                "Chat history not found",
                history_id=history_id
            )
            return []

        return self.chat_sessions[history_id].get("messages", [])

    @app_logger.log_context(component="gemini")
    async def analyze_kubernetes_logs(
        self, logs: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Analyze Kubernetes logs for anomalies and issues.

        Args:
            logs: The logs to analyze (potentially large string).
            context: Additional context about the resource (e.g., pod name, namespace).

        Returns:
            Analysis results or an error dictionary.
        """
        if self.is_api_key_missing():
            error_msg = "Gemini service not initialized"
            app_logger.error(error_msg)
            return {"error": error_msg}

        # Truncate logs if too long to avoid token limits and costs
        max_log_lines = getattr(settings, "GEMINI_MAX_LOG_LINES", 100)  # Use setting
        logs_truncated = False

        log_lines = logs.splitlines()
        if (len(log_lines) > max_log_lines):
            # Take the last N lines
            logs_to_analyze = "\n".join(log_lines[-max_log_lines:])
            logs_truncated = True
            app_logger.debug(
                "Truncated logs for analysis",
                original_lines=len(log_lines),
                truncated_to=max_log_lines
            )
        else:
            logs_to_analyze = logs

        app_logger.debug(
            "Analyzing Kubernetes logs",
            log_lines=len(log_lines),
            truncated=logs_truncated,
            context_provided=context is not None
        )

        system_instruction = """
        You are an expert Kubernetes Site Reliability Engineer (SRE). Analyze the provided logs
        from a Kubernetes resource. Identify critical errors, warning patterns, potential root causes
        for failures (like OOMKilled, CrashLoopBackOff, network issues), and any security concerns.
        Focus on actionable insights. If logs appear normal, state that clearly.
        """

        prompt = f"""
        # Kubernetes Log Analysis Request

        ## Resource Context:
        ```json
        {json.dumps(context or {}, indent=2, default=str)}
        ```

        ## Logs{' (Truncated - Last ' + str(max_log_lines) + ' Lines)' if logs_truncated else ''}:
        ```
        {logs_to_analyze}
        ```

        ## Analysis Task:
        Analyze these logs for potential issues and anomalies based on your SRE expertise.
        Highlight errors, warnings, and recurring patterns.
        Suggest possible root causes if problems are detected.
        If the logs look normal, state that clearly.
        Provide a concise summary of findings.
        """

        try:
            # Create content parts
            content_parts = [
                {
                    "role": "model",  # Changed from "system" to "model" which is a valid role
                    "parts": [{"text": system_instruction}]
                },
                {
                    "role": "user",
                    "parts": [{"text": prompt}]
                }
            ]
            
            # Configure generation settings
            generation_config = types.GenerateContentConfig(
                temperature=0.2,
                top_p=0.95,
                top_k=64,
                candidate_count=1,
                max_output_tokens=4096,
            )
            
            # Generate content using new client API
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model_type,
                contents=content_parts,
                config=generation_config
            )

            analysis_text = response.text if hasattr(response, "text") else "Analysis not available"

            app_logger.debug(
                "Log analysis completed",
                analysis_length=len(analysis_text),
                logs_truncated=logs_truncated
            )

            return {"analysis": analysis_text, "logs_truncated": logs_truncated}

        except Exception as e:
            app_logger.error(
                "Error analyzing logs",
                exception=e
            )
            return {"error": f"Failed to analyze logs: {str(e)}"}

    @app_logger.log_context(component="gemini", operation="analyze_anomaly")
    async def analyze_anomaly(
        self,
        anomaly_data: Dict[str, Any],
        entity_info: Dict[str, Any],
        related_metrics: Dict[str, List[Dict[str, Any]]],
        additional_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze anomaly data using Gemini's function calling capabilities.

        Args:
            anomaly_data: Anomaly data including metrics, alerts, etc.
            entity_info: Information about the affected entity
            related_metrics: Related metrics data
            additional_context: Additional context for the analysis

        Returns:
            Analysis results using AnomalyAnalysis structure
        """
        if not self.client or not self.api_key:
            error_msg = "Gemini service not initialized"
            logger.error(error_msg)
            return self._create_error_response(error_msg)

        operation_id = str(uuid.uuid4())[:8]
        logger.info(
            f"[OperationID:{operation_id}] Analyzing anomaly with Gemini for {entity_info.get('entity_type', 'unknown')}/{entity_info.get('entity_id', 'unknown')}"
        )

        try:
            # Format prompt in a way that highlights important information
            entity_type = entity_info.get("entity_type", "unknown")
            entity_id = entity_info.get("entity_id", "unknown")
            namespace = entity_info.get("namespace", "default")
            
            # Extract anomaly metrics data
            anomaly_score = anomaly_data.get("anomaly_score", 0.0)
            metric_snapshot = anomaly_data.get("metric_snapshot", [])
            
            # Format the metrics data
            metrics_text = "## Metrics Data:\n"
            for metric in metric_snapshot:
                metric_name = metric.get("metric_name", "unknown")
                value = metric.get("value", "N/A")
                unit = metric.get("unit", "")
                timestamp = metric.get("timestamp", "")
                metrics_text += f"- {metric_name}: {value}{unit} ({timestamp})\n"
            
            # Add related metrics if available
            if related_metrics:
                metrics_text += "\n## Related Metrics:\n"
                for metric_type, metrics in related_metrics.items():
                    metrics_text += f"### {metric_type.upper()} Metrics:\n"
                    for metric in metrics[:5]:  # Limit to first 5 metrics
                        metric_name = metric.get("metric_name", "unknown")
                        value = metric.get("value", "N/A")
                        unit = metric.get("unit", "")
                        metrics_text += f"- {metric_name}: {value}{unit}\n"
            
            # Build context from additional_context
            context_text = ""
            if additional_context:
                context_text = "\n## Additional Context:\n"
                for key, value in additional_context.items():
                    if isinstance(value, dict):
                        context_text += f"### {key.replace('_', ' ').title()}:\n"
                        for sub_key, sub_value in value.items():
                            context_text += f"- {sub_key}: {sub_value}\n"
                    else:
                        context_text += f"- {key}: {value}\n"
            
            # Construct the prompt
            prompt = f"""
            # Kubernetes Anomaly Analysis Request
            
            I need a detailed analysis of an anomaly detected in a Kubernetes cluster.
            
            ## Entity Information:
            - Type: {entity_type}
            - Name: {entity_id}
            - Namespace: {namespace}
            - Anomaly Score: {anomaly_score}
            
            {metrics_text}
            
            {context_text}
            
            Please analyze this anomaly data and provide insights on the root cause, potential impact, severity, 
            and specific recommendations for investigation.
            """
            
            # Now use the function calling capability
            try:
                result = await self._call_gemini_with_functions(
                    prompt=prompt,
                    function_declarations=self._function_declarations["analyze_anomaly"],
                    system_instruction=self.system_instructions["k8s_expert"],
                    model_type=ModelType.PRO
                )
                
                # Check if we got function call results
                if result.get("function_call") and result["function_call"].get("args"):
                    function_args = result["function_call"]["args"]
                    
                    # Validate with AnomalyAnalysis model
                    try:
                        analysis = AnomalyAnalysis(
                            root_cause=function_args.get("root_cause", "Unknown root cause"),
                            impact=function_args.get("impact", "Unknown impact"),
                            severity=function_args.get("severity", "Medium"),
                            confidence=function_args.get("confidence", 0.7),
                            recommendations=function_args.get("recommendations", ["Investigate further"])
                        )
                        
                        logger.info(
                            f"[OperationID:{operation_id}] Successfully analyzed anomaly using function calling",
                            severity=analysis.severity,
                            confidence=analysis.confidence
                        )
                        
                        return analysis.dict()
                    except ValidationError as ve:
                        logger.warning(f"Validation error for function call results: {ve}")
                        # Fall back to traditional method
            except Exception as func_error:
                logger.warning(
                    f"[OperationID:{operation_id}] Function calling failed, falling back to traditional method: {func_error}"
                )
            
            # Fall back to traditional method if function calling fails
            return await self._call_gemini_with_model(
                prompt=prompt,
                output_model=AnomalyAnalysis,
                model_type=ModelType.PRO,
                system_instruction=self.system_instructions["k8s_expert"]
            )

        except Exception as e:
            error_msg = f"Error analyzing anomaly: {str(e)}"
            logger.error(f"[OperationID:{operation_id}] {error_msg}", exc_info=True)
            return self._create_error_response(error_msg)

    @app_logger.log_context(component="gemini")
    async def suggest_remediation(
        self,
        anomaly_context: Union[AnomalyAnalysis, Dict[str, Any]],
        failure_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Suggest remediation steps for an anomaly using function calling.

        Args:
            anomaly_context: Anomaly analysis results from analyze_anomaly
            failure_context: Optional additional failure context data

        Returns:
            Remediation steps in a structured format
        """
        if not self.client or not self.api_key:
            error_msg = "Gemini service not initialized"
            logger.error(error_msg)
            return self._create_error_response(error_msg)

        operation_id = str(uuid.uuid4())[:8]
        logger.info(
            f"[OperationID:{operation_id}] Generating remediation suggestions"
        )

        try:
            # Convert anomaly_context to dict if it's a Pydantic model
            if isinstance(anomaly_context, AnomalyAnalysis):
                anomaly_data = anomaly_context.dict()
            else:
                anomaly_data = anomaly_context

            # Format all the context data for the prompt
            root_cause = anomaly_data.get("root_cause", "Unknown issue")
            impact = anomaly_data.get("impact", "Unknown impact")
            severity = anomaly_data.get("severity", "Medium")
            recommendations = anomaly_data.get("recommendations", [])

            # Add failure context if provided
            failure_info = ""
            if failure_context:
                failure_info = "\n## Failure Details:\n"
                for key, value in failure_context.items():
                    if isinstance(value, dict):
                        failure_info += f"### {key}:\n"
                        for sub_key, sub_value in value.items():
                            failure_info += f"- {sub_key}: {sub_value}\n"
                    else:
                        failure_info += f"- {key}: {value}\n"

            # Format recommendations as a list for the prompt
            recommendations_text = "\n".join([f"- {rec}" for rec in recommendations])

            # Construct the prompt
            prompt = f"""
            # Kubernetes Remediation Request

            I need specific remediation steps for the following Kubernetes issue:

            ## Root Cause Analysis:
            - Root cause: {root_cause}
            - Impact: {impact}
            - Severity: {severity}

            ## Investigative Recommendations:
            {recommendations_text}

            {failure_info}

            Please suggest specific, executable remediation commands or steps to fix this issue.
            Also include potential risks associated with these steps, and what a successful outcome looks like.
            """

            # Try using function calling first
            try:
                result = await self._call_gemini_with_functions(
                    prompt=prompt,
                    function_declarations=self._function_declarations["suggest_remediation"],
                    system_instruction=self.system_instructions["remediation_agent"],
                    model_type=ModelType.PRO
                )
                
                # Check if we got function call results
                if result.get("function_call") and result["function_call"].get("args"):
                    function_args = result["function_call"]["args"]
                    
                    # Validate with RemediationSteps model
                    try:
                        remediation = RemediationSteps(
                            steps=function_args.get("steps", ["No steps available"]),
                            risks=function_args.get("risks", ["Unknown risks"]),
                            expected_outcome=function_args.get("expected_outcome", "Resolution of the issue"),
                            confidence=function_args.get("confidence", 0.7)
                        )
                        
                        logger.info(
                            f"[OperationID:{operation_id}] Successfully generated remediation using function calling",
                            step_count=len(remediation.steps),
                            confidence=remediation.confidence
                        )
                        
                        return remediation.dict()
                    except ValidationError as ve:
                        logger.warning(f"Validation error for function call results: {ve}")
                        # Fall back to traditional method
            except Exception as func_error:
                logger.warning(
                    f"[OperationID:{operation_id}] Function calling failed, falling back to traditional method: {func_error}"
                )
            
            # Fall back to traditional method if function calling fails
            return await self._call_gemini_with_model(
                prompt=prompt,
                output_model=RemediationSteps,
                model_type=ModelType.PRO,
                system_instruction=self.system_instructions["remediation_agent"]
            )

        except Exception as e:
            error_msg = f"Error suggesting remediation: {str(e)}"
            logger.error(f"[OperationID:{operation_id}] {error_msg}", exc_info=True)
            return self._create_error_response(error_msg)

    @app_logger.log_context(component="gemini")
    async def verify_remediation(
        self,
        verification_data: Dict[str, Any],
        metrics_after: Dict[str, Any],
        remediation_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Verify if a remediation was successful using function calling.

        Args:
            verification_data: Data about what was remediated
            metrics_after: Metrics after remediation
            remediation_context: Additional context about the remediation attempt

        Returns:
            Verification result in a structured format
        """
        if self.is_api_key_missing():
            error_msg = "Gemini service not initialized"
            logger.error(error_msg)
            return self._create_error_response(error_msg)

        operation_id = str(uuid.uuid4())[:8]
        logger.info(
            f"[OperationID:{operation_id}] Verifying remediation success"
        )

        try:
            # Format the metrics data in a more readable format
            metrics_text = "## Metrics After Remediation:\n"
            for metric_name, metric_data in metrics_after.items():
                if isinstance(metric_data, dict):
                    metrics_text += f"### {metric_name}:\n"
                    for key, value in metric_data.items():
                        metrics_text += f"- {key}: {value}\n"
                elif isinstance(metric_data, list):
                    metrics_text += f"### {metric_name}:\n"
                    for item in metric_data[:5]:  # Limit to first 5 items
                        if isinstance(item, dict):
                            for key, value in item.items():
                                metrics_text += f"- {key}: {value}\n"
                        else:
                            metrics_text += f"- {item}\n"
                else:
                    metrics_text += f"- {metric_name}: {metric_data}\n"

            # Format remediation context
            context_text = ""
            if remediation_context:
                context_text = "\n## Remediation Context:\n"
                for key, value in remediation_context.items():
                    if isinstance(value, dict):
                        context_text += f"### {key}:\n"
                        for sub_key, sub_value in value.items():
                            context_text += f"- {sub_key}: {sub_value}\n"
                    else:
                        context_text += f"- {key}: {value}\n"

            # Format verification data
            verification_text = "## Verification Data:\n"
            for key, value in verification_data.items():
                if isinstance(value, dict):
                    verification_text += f"### {key}:\n"
                    for sub_key, sub_value in value.items():
                        verification_text += f"- {sub_key}: {sub_value}\n"
                else:
                    verification_text += f"- {key}: {value}\n"

            # Construct the prompt
            prompt = f"""
            # Kubernetes Remediation Verification Request

            Please evaluate if the remediation was successful based on the following information:

            {verification_text}

            {metrics_text}

            {context_text}

            Please analyze if the remediation was successful and explain your reasoning.
            """

            # Try using function calling first
            try:
                result = await self._call_gemini_with_functions(
                    prompt=prompt,
                    function_declarations=self._function_declarations["verify_remediation"],
                    system_instruction=self.system_instructions["k8s_expert"],
                    model_type=ModelType.PRO
                )
                
                # Check if we got function call results
                if result.get("function_call") and result["function_call"].get("args"):
                    function_args = result["function_call"]["args"]
                    
                    # Validate with VerificationResult model
                    try:
                        verification = VerificationResult(
                            success=function_args.get("success", False),
                            reasoning=function_args.get("reasoning", "Insufficient information to determine success"),
                            confidence=function_args.get("confidence", 0.5)
                        )
                        
                        logger.info(
                            f"[OperationID:{operation_id}] Successfully verified remediation using function calling",
                            success=verification.success,
                            confidence=verification.confidence
                        )
                        
                        return verification.dict()
                    except ValidationError as ve:
                        logger.warning(f"Validation error for function call results: {ve}")
                        # Fall back to traditional method
            except Exception as func_error:
                logger.warning(
                    f"[OperationID:{operation_id}] Function calling failed, falling back to traditional method: {func_error}"
                )
            
            # Fall back to traditional method if function calling fails
            return await self._call_gemini_with_model(
                prompt=prompt,
                output_model=VerificationResult,
                model_type=ModelType.PRO,
                system_instruction=self.system_instructions["k8s_expert"]
            )

        except Exception as e:
            error_msg = f"Error verifying remediation: {str(e)}"
            logger.error(f"[OperationID:{operation_id}] {error_msg}", exc_info=True)
            return self._create_error_response(error_msg)

    def _create_error_response(self, error_msg: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Create a standardized error response.
        
        Args:
            error_msg: Error message
            details: Optional additional details
            
        Returns:
            Dictionary with error information
        """
        response = {
            "error": error_msg,
            "status": "failed",
            "timestamp": datetime.utcnow().isoformat()
        }
        
        if details:
            response["details"] = details
            
        return response

    @app_logger.log_context(component="gemini")
    async def analyze_and_suggest_for_pod_failure(
        self,
        pod_name: str,
        namespace: str = "default",
        failure_reason: Optional[str] = None,
        pod_logs: Optional[str] = None,
        pod_details: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Analyze pod failure and suggest remediation all in one step.
        
        This method fetches logs (if not provided), analyzes them, determines the root cause,
        and suggests remediation steps for a failing pod.
        
        Args:
            pod_name: Name of the failing pod
            namespace: Namespace of the pod (default: "default")
            failure_reason: Known failure reason (e.g., ImagePullBackOff, CrashLoopBackOff)
            pod_logs: Optional pod logs (if already fetched)
            pod_details: Optional additional pod details
            
        Returns:
            Dictionary containing analysis and remediation suggestions
        """
        operation_id = str(uuid.uuid4())[:8]
        app_logger.info(
            f"[OperationID:{operation_id}] Analyzing pod failure and generating remediation",
            pod=pod_name,
            namespace=namespace,
            failure_reason=failure_reason
        )
        
        if not self.client or not self.api_key:
            error_msg = "Gemini service not initialized"
            app_logger.error(error_msg)
            return self._create_error_response(error_msg)
        
        # Build context information
        context = {
            "pod_name": pod_name,
            "namespace": namespace,
            "failure_reason": failure_reason or "Unknown",
        }
        
        if pod_details:
            context.update(pod_details)
        
        # Analyze logs if provided
        logs_analysis = {}
        if pod_logs:
            logs_analysis = await self.analyze_kubernetes_logs(pod_logs, context)
        
        # Construct comprehensive prompt for remediation
        system_instruction = """
        You are KubeWise AI, an expert in Kubernetes troubleshooting and remediation.
        Your task is to analyze a failing pod, determine the likely root cause based on the information provided,
        and suggest specific remediation steps. Focus on practical, actionable solutions.
        """
        
        prompt = f"""
        # Kubernetes Pod Failure Remediation Request
        
        ## Pod Details:
        - Pod Name: {pod_name}
        - Namespace: {namespace}
        - Failure Reason: {failure_reason or "Unknown"}
        
        ## Additional Context:
        ```json
        {json.dumps(context, indent=2, default=str)}
        ```
        
        """
        
        # Add logs analysis if available
        if logs_analysis and "analysis" in logs_analysis:
            prompt += f"""
            ## Logs Analysis:
            {logs_analysis.get("analysis", "No logs analysis available")}
            """
        elif logs_analysis and "error" in logs_analysis:
            prompt += f"""
            ## Logs Analysis Error:
            {logs_analysis.get("error", "Unknown error during log analysis")}
            """
        else:
            prompt += """
            ## Logs:
            No logs were available for analysis.
            """
        
        # Add specific guidance based on the failure reason
        if failure_reason:
            common_failures = {
                "ImagePullBackOff": """
                This usually indicates an issue with pulling the container image. Common causes include:
                1. Incorrectly specified image name or tag
                2. Private repository requiring authentication
                3. Missing or invalid image pull secrets
                4. Network connectivity issues to the registry
                """,
                
                "CrashLoopBackOff": """
                This indicates the container is repeatedly crashing. Common causes include:
                1. Application errors within the container
                2. Missing dependencies or configuration
                3. Resource constraints (OOM kills)
                4. Liveness probe failures
                """,
                
                "PodInitializing": """
                This indicates issues during pod initialization. Common causes include:
                1. Init container failures
                2. Volume mount issues
                3. ConfigMap or Secret mount problems
                """,
                
                "ContainerCreating": """
                This indicates issues creating the container. Common causes include:
                1. Node resource constraints
                2. Volume attachment issues
                3. Network plugin issues
                """,
                
                "ErrImagePull": """
                This indicates an error pulling the container image. Common causes include:
                1. Non-existent image or tag
                2. Authentication issues with private repositories
                3. Network connectivity issues
                """,
                
                "OOMKilled": """
                This indicates the container was terminated due to Out of Memory. Common causes include:
                1. Memory limits set too low
                2. Memory leaks in the application
                3. High memory usage spikes
                """,
            }
            
            for error_key, guidance in common_failures.items():
                if error_key.lower() in failure_reason.lower():
                    prompt += f"""
                    ## Common Causes for {error_key}:
                    {guidance}
                    """
                    break
        
        prompt += """
        ## Task:
        1. Determine the most likely root cause of the failure based on the information provided
        2. Suggest specific remediation steps with exact commands where appropriate
        3. Provide any additional diagnostic commands that might help gather more information
        4. Assess the potential risks of applying the suggested remediation
        
        Please provide your analysis and recommendations in a clear, structured format.
        """
        
        try:
            # Try function calling approach first
            try:
                result = await self._call_gemini_with_functions(
                    prompt=prompt,
                    function_declarations=self._function_declarations["suggest_remediation"],
                    system_instruction=system_instruction,
                    model_type=ModelType.PRO
                )
                
                # Check if we got function call results
                if result.get("function_call") and result["function_call"].get("args"):
                    function_args = result["function_call"]["args"]
                    
                    # Validate with RemediationSteps model
                    try:
                        remediation = RemediationSteps(
                            steps=function_args.get("steps", ["No steps available"]),
                            risks=function_args.get("risks", ["Unknown risks"]),
                            expected_outcome=function_args.get("expected_outcome", "Resolution of the issue"),
                            confidence=function_args.get("confidence", 0.7)
                        )
                        
                        # Add logs analysis to the response
                        response_data = remediation.dict()
                        if logs_analysis and "analysis" in logs_analysis:
                            response_data["logs_analysis"] = logs_analysis.get("analysis")
                        
                        app_logger.info(
                            f"[OperationID:{operation_id}] Successfully generated remediation using function calling",
                            step_count=len(remediation.steps),
                            confidence=remediation.confidence
                        )
                        
                        return response_data
                    except ValidationError as ve:
                        app_logger.warning(f"Validation error for function call results: {ve}")
                        # Fall back to traditional method
            except Exception as func_error:
                app_logger.warning(
                    f"[OperationID:{operation_id}] Function calling failed, falling back to traditional method: {func_error}"
                )
            
            # Fall back to traditional method
            remediation_result = await self._call_gemini_with_model(
                prompt=prompt,
                output_model=RemediationSteps,
                model_type=ModelType.PRO,
                system_instruction=system_instruction
            )
            
            # Add logs analysis to the response
            if logs_analysis and "analysis" in logs_analysis:
                remediation_result["logs_analysis"] = logs_analysis.get("analysis")
            
            return remediation_result
            
        except Exception as e:
            error_msg = f"Error generating remediation for pod failure: {str(e)}"
            app_logger.error(f"[OperationID:{operation_id}] {error_msg}", exc_info=True)
            return self._create_error_response(error_msg)

    # Add new helper method to fetch pod logs
    async def fetch_pod_logs(self, pod_name: str, namespace: str = "default") -> Dict[str, Any]:
        """
        Fetch logs for a pod using the Kubernetes API.
        
        Args:
            pod_name: Name of the pod
            namespace: Namespace of the pod
            
        Returns:
            Dictionary containing logs or error information
        """
        try:
            from kubernetes import client, config
            import kubernetes.client.rest
            
            # Try to load kube config (will work if running inside cluster or with kubeconfig)
            try:
                config.load_incluster_config()
            except:
                try:
                    config.load_kube_config()
                except:
                    return {"error": "Failed to load Kubernetes configuration"}
            
            # Create API client
            api = client.CoreV1Api()
            
            # Fetch logs
            logs = api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=100  # Limit to last 100 lines for efficiency
            )
            
            return {"logs": logs, "success": True}
            
        except kubernetes.client.rest.ApiException as e:
            return {"error": f"API error getting logs for pod {namespace}/{pod_name}: {e.status}: {e.reason}", "success": False}
        except Exception as e:
            return {"error": f"Error getting logs for pod {namespace}/{pod_name}: {str(e)}", "success": False}
