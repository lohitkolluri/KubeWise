import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

import aiohttp
from google import genai
from loguru import logger
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

# Import RemediationAction from models
from app.models.k8s_models import RemediationAction
# Import k8s_executor to get command templates
from app.utils.k8s_executor import k8s_executor

T = TypeVar("T")


# Define model types
class ModelType(str, Enum):
    PRO = "gemini-2.5-flash-preview-04-17"
    FLASH = "gemini-2.5-flash-preview-04-17"
    FLASH_LITE = "gemini-2.5-flash-preview-04-17"
    PRO_VISION = "gemini-2.0-pro-vision"
    PRO_THINKING = "gemini-2.5-pro-preview"



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


class GeminiService:
    """Service to interact with Google's Gemini AI using the latest Gemini API best practices."""

    def __init__(
        self,
        api_key: str,
        model_type: str = "gemini-1.5-pro",
        core_metric_types: Optional[List[str]] = None,
    ):
        """Initialize the Gemini service using genai.Client.

        Args:
            api_key: Google AI API key
            model_type: Type of Gemini model to use
            core_metric_types: List of core metric types to analyze
        """
        self.api_key = api_key
        self.model_type = model_type
        self.enabled = False
        self.core_metric_types = core_metric_types or []
        self.client = None
        # Initialize chat_sessions dictionary
        self.chat_sessions = {}

        try:
            if not api_key:
                logger.warning("No Gemini API key provided, service will be disabled")
                return
            self.client = genai.Client(api_key=api_key)

            # Add model as a compatibility property
            # This ensures backward compatibility with code that accesses the model attribute
            self.model = self.client

            logger.info("Gemini AI client initialized successfully")
            self.enabled = True
        except Exception as e:
            logger.exception(f"Failed to initialize Gemini AI client: {e}")
            self.enabled = False

    def is_api_key_missing(self) -> bool:
        """Check if the API key is missing."""
        return not self.enabled

    @retry(
        retry=retry_if_exception_type(
            (ConnectionError, TimeoutError, aiohttp.ClientError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_fixed(2),
    )
    async def _call_gemini_with_model(
        self,
        prompt: str,
        output_model: Type[BaseModel],
        model_type: Optional[ModelType] = None,
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> dict:
        """
        Call Gemini API with a specific output model using function calling.

        Args:
            prompt: The prompt to send to Gemini
            output_model: The Pydantic model class to parse the response into
            model_type: The type of model to use (optional)
            system_instruction: System instruction to include (optional)
            **kwargs: Additional keyword arguments to pass to the model

        Returns:
            The parsed model response or an error dict
        """
        try:
            if not self.enabled or not self.client:
                logger.error("Gemini client is not initialized.")
                return {"error": "Gemini client is not initialized."}

            model_name = model_type.value if model_type else self.model_type

            # Create generation config
            config_params = {
                "temperature": kwargs.get("temperature", 0.2),
                "top_p": kwargs.get("top_p", 0.95),
                "top_k": kwargs.get("top_k", 64),
                "max_output_tokens": kwargs.get("max_output_tokens", 4096),
            }

            # Include candidate_count if provided
            if "candidate_count" in kwargs:
                config_params["candidate_count"] = kwargs["candidate_count"]

            # Add function calling tools if provided
            if "tools" in kwargs:
                config_params["tools"] = kwargs["tools"]

            # Use structured JSON output for Pydantic models
            if output_model:
                config_params["response_mime_type"] = "application/json"
                config_params["response_schema"] = output_model

            config = genai.types.GenerateContentConfig(**config_params)

            # Create content with system instruction if provided
            contents = []
            if system_instruction:
                contents.append({
                    "role": "system",
                    "parts": [{"text": system_instruction}]
                })

            # Add user prompt
            contents.append({
                "role": "user",
                "parts": [{"text": prompt}]
            })

            # Make the API call
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=model_name,
                contents=contents,
                config=config,
            )

            # Process the response to extract content
            # Structured parsing: use SDK's parsed attribute if available
            if hasattr(response, "parsed") and response.parsed is not None:
                parsed = response.parsed
                return parsed.model_dump() if isinstance(parsed, BaseModel) else parsed

            # Fallback to manual JSON extraction
            if response and hasattr(response, "text"):
                return self._extract_json_from_text(response.text, output_model)

            logger.error("Invalid response format from Gemini API")
            return {"error": "Invalid response format from Gemini API"}

        except Exception as e:
            logger.error(f"Error calling Gemini API with model: {str(e)}", exc_info=True)
            return {"error": f"Error calling Gemini API with model: {str(e)}"}

    def _fix_validation_errors(self, data: dict, output_model: Type[BaseModel], validation_error: ValidationError) -> dict:
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
                # Handle missing field errors
                if error_type == "missing":
                    # Set default values
                    field_info = output_model.model_fields.get(field)
                    if field_info:
                        annotation = field_info.annotation
                        if annotation is str:
                            fixed_data[field] = "unknown"
                        elif annotation is float:
                            fixed_data[field] = 0.0
                        elif annotation is int:
                            fixed_data[field] = 0
                        elif annotation is bool:
                            fixed_data[field] = False
                        elif hasattr(annotation, "__origin__") and annotation.__origin__ is list:
                            fixed_data[field] = []
                        elif hasattr(annotation, "__origin__") and annotation.__origin__ is dict:
                            fixed_data[field] = {}

                # Handle type conversion errors
                elif error_type in ("float_parsing", "int_parsing", "bool_parsing"):
                    field_info = output_model.model_fields.get(field)
                    if field_info and field in fixed_data:
                        value = fixed_data[field]
                        annotation = field_info.annotation

                        if annotation is float and isinstance(value, (str, int)):
                            try:
                                fixed_data[field] = float(value)
                            except (ValueError, TypeError):
                                fixed_data[field] = 0.0
                        elif annotation is int and isinstance(value, (str, float)):
                            try:
                                fixed_data[field] = int(float(value))
                            except (ValueError, TypeError):
                                fixed_data[field] = 0
                        elif annotation is bool and isinstance(value, str):
                            fixed_data[field] = value.lower() in ("true", "yes", "1")

        # Try to validate the fixed data
        try:
            validated = output_model(**fixed_data)
            return validated.model_dump()
        except ValidationError:
            # If still failing, use minimal valid response
            return self._create_minimal_response(output_model).model_dump()

    async def _extract_function_response(
        self, result, model_class: Type[T]
    ) -> Union[Dict[str, Any], T]:
        """Extract the function response from a Gemini result."""
        try:
            # Extract the function response
            if not result.candidates:
                logger.error("No candidates in Gemini response")
                response = self._create_minimal_response(model_class)
                return (
                    response.model_dump()
                    if isinstance(response, BaseModel)
                    else response
                )

            candidate = result.candidates[0]
            content = (
                candidate.content.parts[0].text
                if hasattr(candidate, "content") and candidate.content.parts
                else "{}"
            )
            logger.debug(f"Raw response: {content}")

            # Try to extract JSON from the text response
            try:
                # Try to find JSON in the response
                matches = re.findall(r"```json\n(.*?)\n```", content, re.DOTALL)
                data = {}
                if matches:
                    data = json.loads(matches[0])
                else:
                    # Try to parse the entire content as JSON
                    try:
                        data = json.loads(content)
                    except json.JSONDecodeError:
                        # Try to extract JSON-like structure from text
                        data = self._extract_json_from_text(content, model_class)
                        if not data:
                            response = self._create_minimal_response(model_class)
                            return (
                                response.model_dump()
                                if isinstance(response, BaseModel)
                                else response
                            )

                # Fix fields manually if needed based on model class
                if model_class == AnomalyAnalysis:
                    # Map fields from Gemini's response format (case-insensitive)
                    field_mappings = {
                        "root_cause": ["Root cause", "RootCause", "root_cause"],
                        "impact": ["Impact", "impact"],
                        "severity": ["Severity", "severity"],
                        "confidence": ["Confidence", "confidence"],
                        "recommendations": [
                            "Recommendations",
                            "recommendations",
                            "Suggested actions",
                            "Actions",
                        ],
                    }

                    processed_data = {}

                    # Try to find each field using various possible keys
                    for target_field, possible_keys in field_mappings.items():
                        # Try each possible key
                        for key in possible_keys:
                            if key in data:
                                value = data[key]
                                # Handle recommendations specially
                                if target_field == "recommendations":
                                    if isinstance(value, str):
                                        processed_data[target_field] = [value]
                                    elif isinstance(value, list):
                                        processed_data[target_field] = value
                                    else:
                                        processed_data[target_field] = []
                                else:
                                    processed_data[target_field] = value
                                break

                        # If field not found, set default value
                        if target_field not in processed_data:
                            if target_field == "recommendations":
                                processed_data[target_field] = []
                            elif target_field == "confidence":
                                processed_data[target_field] = 0.0
                            else:
                                processed_data[target_field] = "unknown"

                    # Try to extract recommendations from text if not found in JSON
                    if not processed_data["recommendations"]:
                        # Look for bullet points or numbered lists in the text
                        recommendations = []
                        lines = content.split("\n")
                        for line in lines:
                            line = line.strip()
                            if (
                                line.startswith("- ")
                                or line.startswith("* ")
                                or re.match(r"^\d+\.", line)
                            ):
                                recommendations.append(line.lstrip("- *").strip())
                        if recommendations:
                            processed_data["recommendations"] = recommendations

                    data = processed_data

                elif model_class == RemediationSteps:
                    # Handle steps field
                    steps = data.get("steps", data.get("Steps", []))
                    if isinstance(steps, str):
                        data["steps"] = [steps]
                    elif steps is None:
                        data["steps"] = []

                    # Handle risks field
                    risks = data.get("risks", data.get("Risks", []))
                    if isinstance(risks, str):
                        data["risks"] = [risks]
                    elif risks is None:
                        data["risks"] = []

                    # Set default values only if fields are missing or empty
                    if not data.get("expected_outcome"):
                        data["expected_outcome"] = "unknown"
                    if "confidence" not in data or not isinstance(
                        data["confidence"], (int, float)
                    ):
                        data["confidence"] = 0.0

                try:
                    # Create the model instance
                    model_instance = model_class(**data)
                    return (
                        model_instance.model_dump()
                        if isinstance(model_instance, BaseModel)
                        else model_instance
                    )
                except ValidationError as ve:
                    logger.warning(f"Validation error with processed data: {ve}")
                    # Try to construct a partial response
                    response = self._construct_partial_response(data, model_class)
                    return (
                        response.model_dump()
                        if isinstance(response, BaseModel)
                        else response
                    )

            except Exception as e:
                logger.warning(f"Failed to extract JSON from text response: {e}")
                response = self._create_minimal_response(model_class)
                return (
                    response.model_dump()
                    if isinstance(response, BaseModel)
                    else response
                )

        except Exception as e:
            logger.error(f"Error extracting function response: {e}", exc_info=True)
            response = self._create_minimal_response(model_class)
            return (
                response.model_dump() if isinstance(response, BaseModel) else response
            )

    def _extract_json_from_text(
        self, text: str, output_model: BaseModel
    ) -> Optional[Dict[str, Any]]:
        """Extract and validate JSON from text response."""
        try:
            # If the text is a simple error message or doesn't contain JSON-like structures,
            # create a minimal valid response
            if not any(char in text for char in "{[]}"):
                logger.debug(
                    f"Text does not contain JSON-like structures: {text[:100]}..."
                )
                return self._create_minimal_response(output_model).model_dump()

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
                    if group.strip():
                        json_candidates.append(group.strip())

            # If no matches, try the whole text if it looks like JSON
            if not json_candidates and (
                text.strip().startswith("{") or text.strip().startswith("[")
            ):
                json_candidates.append(text.strip())

            # Try each candidate
            for json_str in json_candidates:
                try:
                    # Clean up common issues
                    json_str = json_str.replace("\n", " ").replace("\r", "")

                    # Fix common JSON formatting errors
                    json_str = re.sub(
                        r'(?<!")(\w+)(?=":)', r'"\1"', json_str
                    )  # Add quotes to unquoted keys
                    json_str = re.sub(r",\s*}", "}", json_str)  # Remove trailing commas

                    # Parse the JSON
                    data = json.loads(json_str)

                    # Validate with Pydantic
                    try:
                        validated_data = output_model(**data)
                        logger.info(
                            f"Successfully validated extracted JSON against {output_model.__name__}"
                        )
                        return validated_data.model_dump()
                    except ValidationError as ve:
                        logger.debug(f"Validation error for extracted JSON: {ve}")
                        continue
                except json.JSONDecodeError:
                    continue

            logger.warning(f"Could not extract valid JSON from text: {text[:200]}...")
            # Return a minimal valid response instead of None
            return self._create_minimal_response(output_model).model_dump()
        except Exception as e:
            logger.error(f"Error extracting JSON from text: {e}", exc_info=True)
            # Return a minimal valid response instead of None
            return self._create_minimal_response(output_model).model_dump()

    def _pydantic_to_gemini_type(self, field_type: Any) -> str:
        """Convert Pydantic type to Gemini schema type."""
        if field_type is str:
            return "string"
        elif field_type is int:
            return "integer"
        elif field_type is float:
            return "number"
        elif field_type is bool:
            return "boolean"
        elif hasattr(field_type, "__origin__") and field_type.__origin__ is list:
            return "array"
        elif hasattr(field_type, "__origin__") and field_type.__origin__ is dict:
            return "object"
        else:
            # Default to string for complex types
            return "string"

    def _construct_partial_response(
        self, response_data: Dict[str, Any], output_model: BaseModel
    ) -> Dict[str, Any]:
        """Attempt to construct a partially valid response when full validation fails."""
        try:
            # Start with a minimal valid response
            minimal_response = self._create_minimal_response(output_model)

            # Only update with fields that don't cause validation errors
            for field, value in response_data.items():
                if field in minimal_response:
                    try:
                        # Try to validate just this field
                        test_data = minimal_response.copy()
                        test_data[field] = value
                        output_model(**test_data)
                        minimal_response[field] = value
                    except (ValidationError, Exception):
                        # If validation fails, keep the default value
                        pass

            logger.info(
                f"Constructed partial valid response for {output_model.__name__}"
            )
            return minimal_response
        except Exception as e:
            logger.error(f"Error constructing partial response: {e}", exc_info=True)
            return self._create_minimal_response(output_model)

    def _create_minimal_response(self, model_class: Type[T]) -> T:
        """Create a minimal valid response for the given Pydantic model class."""
        try:
            defaults = {}

            # Get model fields
            for name, field in model_class.model_fields.items():
                typ = field.annotation
                if typ is str:
                    defaults[name] = "unknown"
                elif typ is float:
                    defaults[name] = 0.0
                elif typ is int:
                    defaults[name] = 0
                elif typ is bool:
                    defaults[name] = False
                elif typ is list or getattr(typ, "__origin__", None) is list:
                    defaults[name] = []
                elif typ is dict or getattr(typ, "__origin__", None) is dict:
                    defaults[name] = {}
                else:
                    defaults[name] = None

            return model_class(**defaults)
        except Exception as e:
            logger.error(
                f"Error creating minimal response for {model_class.__name__}: {e}"
            )
            # Last resort - try with empty dict
            try:
                return model_class()
            except Exception:
                # If even that fails, raise the original error
                raise

    def _safely_handle_response(self, response, output_model=None, default_return=None):
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
            default_return = self._create_minimal_response(output_model)
            if isinstance(default_return, BaseModel):
                default_return = default_return.model_dump()
        elif default_return is None:
            default_return = {"error": "Invalid response", "details": "No data returned"}

        # Handle None responses
        if response is None:
            logger.warning("Received None response from Gemini API")
            return default_return

        # Handle string responses
        if isinstance(response, str):
            logger.warning(f"Received string response instead of structured data: {response[:100]}...")
            return default_return

        # Handle BaseModel responses - convert to dict
        if isinstance(response, BaseModel):
            try:
                response = response.model_dump()
            except Exception as e:
                logger.error(f"Error converting BaseModel to dict: {e}")
                return default_return

        # Now handle dict responses and check for error conditions
        if isinstance(response, dict):
            # Check for explicit error key
            if "error" in response:
                error_msg = str(response.get("error", "Unknown error"))
                details = str(response.get("details", "No additional details"))
                logger.error(f"Error in Gemini response: {error_msg} - {details}")
                return {"error": error_msg, "details": details}

            # Check for other error indicators
            if response.get("status") == "failed":
                error_msg = "API request failed"
                details = str(response.get("message", "No error message provided"))
                logger.error(f"Failed API response: {error_msg} - {details}")
                return {"error": error_msg, "details": details}

            # No error found, return the response
            return response

        # If we get here, the response is an unhandled type
        logger.warning(f"Unhandled response type: {type(response)}")
        return default_return

    # --- Chat Methods ---
    # (Keep chat methods as they were, assuming they function correctly)
    async def create_chat_session(
        self, system_instruction: Optional[str] = None, history_id: Optional[str] = None
    ) -> Optional[str]:  # Added Optional return type hint
        """
        Create a new chat session with Gemini.

        Args:
            system_instruction: Optional system instructions to include
            history_id: Optional history ID to use (will generate one if not provided)

        Returns:
            The history ID for this chat session or None on failure
        """
        if not self.model or not self.enabled:
            logger.warning("Gemini service not initialized")
            return None

        try:
            # Generate a random ID if none provided
            if not history_id:
                # Use UUID for more robust unique IDs
                history_id = f"chat_{uuid.uuid4()}"

            # Initialize a new chat session
            self.chat_sessions[history_id] = {
                "created_at": datetime.now().isoformat(),  # noqa: F821

                "messages": [],
                "system_instruction": system_instruction,
            }

            logger.info(f"Created chat session with ID: {history_id}")
            return history_id

        except Exception as e:
            logger.error(
                f"Failed to create chat session: {e}", exc_info=True
            )  # Added exc_info
            return None

    async def chat_message(
        self,
        session_id: str,
        message: str,
        chat_history: List[Dict[str, Any]],
        context_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Process a user chat message and generate a response."""
        logger.debug(f"Processing chat message for session {session_id}")

        if not self.model or not self.enabled:
            return self._create_minimal_response(ChatResponse)

        # Format the chat history
        formatted_history = []
        for entry in chat_history:
            role = entry.get("role", "unknown")
            content = entry.get("content", "")
            formatted_history.append(f"{role}: {content}")

        # Format context data if available
        context_str = ""
        if context_data:
            context_sections = []

            # Cluster information
            if "cluster" in context_data:
                cluster_info = context_data["cluster"]
                context_sections.append("## Cluster Information:")
                for key, value in cluster_info.items():
                    context_sections.append(f"- {key}: {value}")

            # Anomalies information
            if "recent_anomalies" in context_data:
                anomalies = context_data["recent_anomalies"]
                context_sections.append(f"\n## Recent Anomalies ({len(anomalies)}):")
                for i, anomaly in enumerate(anomalies[:5]):  # Limit to 5 most recent
                    context_sections.append(f"### Anomaly {i+1}:")
                    for key, value in anomaly.items():
                        if isinstance(value, dict):
                            context_sections.append(f"- {key}: {json.dumps(value)}")
                        else:
                            context_sections.append(f"- {key}: {value}")

            # Resource metrics
            if "resource_metrics" in context_data:
                metrics = context_data["resource_metrics"]
                context_sections.append("\n## Resource Metrics:")
                for resource, resource_metrics in metrics.items():
                    context_sections.append(f"### {resource}:")
                    for metric_name, metric_value in resource_metrics.items():
                        context_sections.append(f"- {metric_name}: {metric_value}")

            context_str = "\n".join(context_sections)

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

        logger.debug(f"Calling Gemini for chat response for session {session_id}")
        # Use flash-lite model for this lightweight conversation task
        return await self._call_gemini_with_model(
            prompt, ChatResponse, ModelType.FLASH_LITE
        )

    async def get_chat_history(self, history_id: str) -> List[Dict[str, Any]]:
        """Get the history of a chat session."""
        if (history_id not in self.chat_sessions):
            logger.warning(f"Chat history not found for ID: {history_id}")
            return []

        return self.chat_sessions[history_id].get("messages", [])  # Safely get messages

    async def analyze_kubernetes_logs(
        self, logs: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:  # Added Optional hint
        """
        Analyze Kubernetes logs for anomalies and issues.

        Args:
            logs: The logs to analyze (potentially large string).
            context: Additional context about the resource (e.g., pod name, namespace).

        Returns:
            Analysis results or an error dictionary.
        """
        if not self.model or not self.enabled:
            return {"error": "Gemini service not initialized"}

        # Truncate logs if too long to avoid token limits and costs
        from app.core import config as settings  # noqa: F821
        max_log_lines = getattr(settings, "GEMINI_MAX_LOG_LINES", 100)  # Use setting
        logs_truncated = False

        log_lines = logs.splitlines()
        if len(log_lines) > max_log_lines:
            # Take the last N lines
            logs_to_analyze = "\n".join(log_lines[-max_log_lines:])
            logs_truncated = True
            logger.debug(f"Truncated logs to last {max_log_lines} lines for analysis.")
        else:
            logs_to_analyze = logs

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
            # Use the default generation config
            response = await asyncio.to_thread(
                self.client.models.generate_content, # Corrected: Use client.models.generate_content
                contents=prompt,
                config=genai.types.GenerateContentConfig( # Changed: 'generation_config' to 'config'
                    temperature=0.2,
                    top_p=0.95,
                    top_k=64,
                    candidate_count=1,
                    max_output_tokens=4096,
                ),
                system_instruction=system_instruction,
            )

            analysis_text = (
                response.text if hasattr(response, "text") else "Analysis not available"
            )

            return {"analysis": analysis_text, "logs_truncated": logs_truncated}

        except Exception as e:
            logger.error(f"Error analyzing logs: {e}", exc_info=True)  # Added exc_info
            return {"error": f"Failed to analyze logs: {str(e)}"}

    async def execute_code_for_kubernetes_analysis(
        self, query: str, context_data: Dict[str, Any], include_libraries: bool = True
    ) -> Dict[str, Any]:
        """
        Execute code (via Gemini Tool) to analyze Kubernetes data.

        Args:
            query: The query prompting code generation (e.g., "Find pods with high restart counts").
            context_data: The Kubernetes context data (e.g., list of pods, deployments) to analyze.
            include_libraries: Whether to hint common libraries for the generated code.

        Returns:
            Results of the code execution including summary, code, output, and visualizations.
        """
        if not self.enabled or not self.client:
            return {"error": "Gemini service not initialized"}

        # Check if Code Execution tool is enabled in settings (optional)
        if not getattr(settings, "GEMINI_ENABLE_CODE_EXECUTION", True):
            return {"error": "Code execution tool is disabled in settings."}

        # Format context data for code execution
        # Use default=str for non-serializable types like datetime
        try:
            context_json = json.dumps(context_data, indent=2, default=str)
        except Exception as json_err:
            logger.error(
                f"Failed to serialize context data for code execution: {json_err}",
                exc_info=True,
            )
            return {"error": f"Failed to serialize context data: {str(json_err)}"}

        libraries_prompt = (
            """
        You can use standard Python libraries, including pandas, numpy, matplotlib, seaborn, and scikit-learn for analysis and visualization.
        Assume the input data is loaded into a variable named `k8s_data`.
        """
            if include_libraries
            else "Use standard Python libraries. Assume the input data is loaded into a variable named `k8s_data`."
        )

        prompt = f"""
        # Kubernetes Data Analysis Task (Code Execution)

        Analyze the following Kubernetes data using Python code:

        Input Data (`k8s_data` variable in your code):
        ```json
        {context_json}
        ```

        Analysis Request: {query}

        {libraries_prompt}

        Generate Python code to perform the requested analysis on the `k8s_data`.
        Your code should:
        1. Process the `k8s_data`.
        2. Perform the analysis as requested.
        3. Print key findings or results.
        4. Optionally, generate simple plots (e.g., histograms, bar charts) if useful, using matplotlib or seaborn.

        I will execute your generated Python code using the Gemini code execution tool.
        Ensure your code outputs the analysis results clearly.
        """

        try:
            # Code execution tool setup
            # Ensure the tool configuration is correct for the API version
            import glm  # noqa: F821

            tool = glm.Tool(code_execution=glm.CodeExecution())  # Using glm structure

            response = await asyncio.to_thread(
                self.client.models.generate_content, # Corrected: Use client.models.generate_content
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    temperature=0.2,
                    top_p=0.95,
                    top_k=64,
                    candidate_count=1,
                    max_output_tokens=4096,
                ),
                tools=[tool],  # Pass tool configuration
            )

            # Extract results and code from the response
            results = {
                "summary": "",  # Text summary from the model
                "code": "",  # The generated Python code
                "execution_result": "",  # Output from the executed code
                "visualizations": [],  # Any generated plots/images
            }

            # Safely extract parts from the response
            if response and hasattr(response, "candidates") and response.candidates:
                # Usually process the first candidate
                candidate = response.candidates[0]
                if (
                    hasattr(candidate, "content")
                    and candidate.content
                    and hasattr(candidate.content, "parts")
                ):
                    for part in candidate.content.parts:
                        # Extract text summary
                        if hasattr(part, "text") and part.text:
                            results["summary"] += part.text + "\n"
                        # Extract executable code
                        if hasattr(part, "executable_code") and part.executable_code:
                            results["code"] = (
                                part.executable_code.code
                            )  # Overwrite if multiple code blocks? Usually one.
                        # Extract code execution result (output)
                        if (
                            hasattr(part, "code_execution_result")
                            and part.code_execution_result
                        ):
                            results["execution_result"] = (
                                part.code_execution_result.output
                            )
                        # Extract inline data (e.g., images) - Requires checking mime_type and decoding data
                        # This part needs careful handling based on expected output types
                        # if hasattr(part, 'inline_data') and part.inline_data:
                        #    results["visualizations"].append({
                        #        "mime_type": part.inline_data.mime_type,
                        #        "data": part.inline_data.data # May need base64 decoding depending on mime_type
                        #    })

            results["summary"] = results["summary"].strip()
            return results

        except Exception as e:
            logger.error(
                f"Error in code execution analysis: {e}", exc_info=True
            )  # Added exc_info
            return {"error": f"Failed to execute code analysis: {str(e)}"}

    async def analyze_anomaly(
        self,
        anomaly_data: Dict[str, Any],
        entity_info: Dict[str, Any],
        related_metrics: Dict[str, List[Dict[str, Any]]],
        additional_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Perform root cause analysis on an anomaly using structured metric data.

        Args:
            anomaly_data: Information about the anomaly event
            entity_info: Information about the affected entity
            related_metrics: Related metrics data around the time of the anomaly
            additional_context: Additional cluster context including historical metrics

        Returns:
            Dictionary with anomaly analysis details including root cause and recommendations
        """
        logger.info(f"Analyzing anomaly for {entity_info.get('entity_id', 'unknown')}")

        if not self.model or not self.enabled:
            error_msg = "AI analysis is not available - Gemini service is not initialized."
            logger.error(error_msg)
            return {"error": error_msg, "status": "failed"}

        try:
            # Extract historical metrics from additional_context if available
            historical_metrics = []
            metric_window = additional_context.get("metric_window", []) if additional_context else []
            if metric_window:
                historical_metrics = metric_window
                logger.info(f"Using {len(historical_metrics)} historical metric snapshots for analysis")

            # Format metrics in a more AI-friendly structured way with clear timestamps
            structured_metrics = {}
            for metric_name, metric_data in related_metrics.items():
                structured_values = []
                for data_point in metric_data:
                    if isinstance(data_point, dict) and "timestamp" in data_point and "value" in data_point:
                        structured_values.append({
                            "timestamp": data_point["timestamp"],
                            "value": data_point["value"]
                        })
                    elif isinstance(data_point, dict) and "values" in data_point:
                        for val in data_point["values"]:
                            if isinstance(val, list) and len(val) >= 2:
                                try:
                                    timestamp = datetime.fromtimestamp(val[0]).isoformat()
                                    structured_values.append({
                                        "timestamp": timestamp,
                                        "value": val[1]
                                    })
                                except Exception:
                                    pass
                structured_metrics[metric_name] = structured_values

            # Create a clear context object with entity status and anomaly details
            context_obj = {
                "entity": {
                    "id": entity_info.get("entity_id", "unknown"),
                    "type": entity_info.get("entity_type", "unknown"),
                    "namespace": entity_info.get("namespace", "default"),
                },
                "anomaly": {
                    "score": anomaly_data.get("anomaly_score", 0),
                    "detection_time": datetime.utcnow().isoformat(),
                    "is_critical": anomaly_data.get("is_critical", False),
                },
                "metrics": structured_metrics,
                "historical_data_available": len(historical_metrics) > 0
            }

            # Add prediction data if available
            if anomaly_data.get("predicted_failures"):
                context_obj["predictions"] = anomaly_data.get("predicted_failures")

            # Format for prompt
            context_str = json.dumps(context_obj, indent=2)

            # Create a system instruction for better results
            system_instruction = """
            You are an expert Kubernetes SRE with deep experience in troubleshooting and root cause analysis.
            Analyze the anomaly data, metrics, and context to determine the likely root cause of the issue.
            Focus on operational insights and actionable recommendations based on Kubernetes best practices.
            Consider all the metrics together to find patterns and correlations.
            """

            # Build a comprehensive prompt with historical context
            prompt = f"""
            # Kubernetes Anomaly Root Cause Analysis Request

            ## Entity Information
            - Type: {entity_info.get("entity_type", "unknown")}
            - ID: {entity_info.get("entity_id", "unknown")}
            - Namespace: {entity_info.get("namespace", "default")}

            ## Anomaly Details
            - Detection Time: {datetime.utcnow().isoformat()}
            - Anomaly Score: {anomaly_data.get("anomaly_score", 0)}
            - Is Critical: {anomaly_data.get("is_critical", False)}

            ## Structured Context (Including Metrics)
            ```json
            {context_str}
            ```

            ## Analysis Instructions:
            1. Analyze patterns across all metrics together, not just individual metrics
            2. Identify likely root cause of this anomaly considering the full context
            3. Assess the impact on the broader system, not just this component
            4. Rate the severity as High, Medium, or Low based on potential impact
            5. Provide specific recommendations for investigation and resolution
            6. Assign a confidence score (0.0-1.0) to your analysis

            Present your analysis as a structured JSON response with the following fields:
            - root_cause: The identified probable root cause of the anomaly
            - impact: The potential impact on the system or application
            - severity: Estimated severity (High, Medium, Low)
            - confidence: Confidence score (0.0-1.0) in the analysis
            - recommendations: List of specific recommendations for investigation
            """

            # Implement robust retry logic for API calls with progressive backoff
            max_retries = 3
            retry_delay = 2  # starting backoff in seconds

            for attempt in range(max_retries):
                try:
                    # Call Gemini API with structured output
                    raw_response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        model=ModelType.PRO.value,
                        contents=[
                            {
                                "role": "system",
                                "parts": [{"text": system_instruction}]
                            },
                            {
                                "role": "user",
                                "parts": [{"text": prompt}]
                            }
                        ],
                        config=genai.types.GenerateContentConfig(
                            temperature=0.2,
                            top_p=0.95,
                            top_k=64,
                            max_output_tokens=2048,
                            response_mime_type="application/json",
                            response_schema=AnomalyAnalysis
                        ),
                    )

                    # Check if we got a valid response
                    if raw_response and hasattr(raw_response, "parsed") and raw_response.parsed is not None:
                        # Use the parsed structured output directly if available
                        parsed_response = raw_response.parsed
                        logger.info(f"Successfully parsed structured AnomalyAnalysis response")
                        return parsed_response.model_dump()
                    elif raw_response and hasattr(raw_response, "text") and raw_response.text.strip():
                        # Try to extract JSON from text if .parsed is not available
                        return self._extract_json_from_text(raw_response.text, AnomalyAnalysis)

                    # If we got here, we don't have a good response
                    logger.warning(f"Attempt {attempt+1}: No valid response from Gemini API")

                    if attempt < max_retries - 1:
                        # Implement exponential backoff
                        backoff_time = retry_delay * (2 ** attempt)
                        logger.info(f"Retrying in {backoff_time} seconds...")
                        await asyncio.sleep(backoff_time)
                        continue
                    else:
                        logger.error("Max retries exceeded, returning minimal response")
                        return self._create_minimal_response(AnomalyAnalysis).model_dump()

                except Exception as api_error:
                    logger.error(f"API call to Gemini failed in analyze_anomaly (attempt {attempt+1}): {str(api_error)}")

                    # Check for rate limiting errors
                    error_msg = str(api_error).lower()
                    if any(err in error_msg for err in ["quota", "rate limit", "429", "too many requests"]):
                        if attempt < max_retries - 1:
                            backoff_time = retry_delay * (2 ** attempt)  # Exponential backoff
                            logger.info(f"Rate limited. Backing off for {backoff_time} seconds...")
                            await asyncio.sleep(backoff_time)
                            continue

                    # For other errors, also try to retry with backoff
                    if attempt < max_retries - 1:
                        backoff_time = retry_delay * (2 ** attempt)
                        logger.info(f"Retrying after error in {backoff_time} seconds...")
                        await asyncio.sleep(backoff_time)
                    else:
                        logger.error("Max retries exceeded after errors, returning minimal response")
                        return self._create_minimal_response(AnomalyAnalysis).model_dump()

            # Fallback response if all retries failed but we somehow got here
            return self._create_minimal_response(AnomalyAnalysis).model_dump()

        except Exception as e:
            logger.error(f"Error during anomaly analysis: {e}", exc_info=True)
            return self._create_minimal_response(AnomalyAnalysis).model_dump()

    async def suggest_remediation(
        self,
        anomaly_context: Union[AnomalyAnalysis, Dict[str, Any]],
        failure_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Suggest remediation steps for an anomaly.

        Args:
            anomaly_context: The anomaly analysis or context dictionary
            failure_context: Optional failure context dictionary

        Returns:
            Dictionary containing remediation steps and explanations (conforming to RemediationSteps model)
        """
        # Define a fallback response upfront
        fallback_response = {
            "steps": [], # Return an empty list of steps if generation fails
            "risks": ["Manual intervention may be required."],
            "expected_outcome": "Resolution of the identified issue",
            "confidence": 0.0,
            "error": "Failed to generate specific remediation steps." # Add an error field for clarity
        }

        try:
            # Check if input is missing
            if not anomaly_context:
                logger.warning("Missing anomaly context for remediation suggestion")
                return fallback_response

            # Convert dictionary to AnomalyAnalysis if needed
            if isinstance(anomaly_context, dict):
                try:
                    # Try to convert to AnomalyAnalysis if it has the right fields
                    if "root_cause" in anomaly_context:
                        analysis_for_prompt = anomaly_context
                    else:
                        # Use the raw context as is
                        analysis_for_prompt = anomaly_context
                except Exception as e:
                    logger.warning(f"Could not convert context to AnomalyAnalysis: {e}")
                    analysis_for_prompt = anomaly_context
            else:
                # Use the existing AnomalyAnalysis model
                analysis_for_prompt = anomaly_context.model_dump() if hasattr(anomaly_context, "model_dump") else anomaly_context.__dict__

            # Extract metrics if available (this is useful for the prompt)
            metrics_summary = "No specific metrics provided."
            if isinstance(anomaly_context, dict) and "metrics_snapshot" in anomaly_context:
                try:
                    metrics = anomaly_context.get("metrics_snapshot", [])
                    if metrics and len(metrics) > 0:
                        metrics_summary = f"Metrics snapshot contains {len(metrics)} data points."
                except Exception:
                    pass

            # Get available command templates from k8s_executor
            available_commands_info = k8s_executor.get_command_templates_info()
            command_list_str = "\n".join([
                f"- **{name}**: {info['description']} (Required: {', '.join(info['required_params'])}, Optional: {', '.join(info['optional_params'])})"
                for name, info in available_commands_info.items()
            ])

            # Build the prompt - **Significantly revised structure**
            prompt = f"""
            # Kubernetes Remediation Suggestion Request

            Analyze the following anomaly or failure context and provide highly relevant remediation steps using the provided COMMAND TEMPLATES.

            ## Context Information:
            ```json
            {json.dumps(analysis_for_prompt, indent=2, default=str)}
            ```"""

            # Add metrics summary if available
            if metrics_summary:
                prompt += f"\n\n## Metrics Summary:\n{metrics_summary}"

            # Add failure context if provided
            if failure_context:
                prompt += f"\n\n## Failure Context:\n```json\n{json.dumps(failure_context, indent=2, default=str)}\n```"

            # Continue with the rest of the prompt
            prompt += f"""

            ## Available Remediation Command Templates:
            Use ONLY these specific action types and their parameters:
            {command_list_str}

            ## Your Task:
            1. Identify the best 1-3 remediation actions from the "Available Remediation Command Templates" that are most likely to resolve the issue described in the "Context Information".
            2. Prioritize the actions from most recommended to least recommended.
            3. For each recommended action, create a structured object using the `RemediationAction` format described below.
            4. Carefully populate the `action_type`, `resource_type`, `resource_name`, `namespace`, and `parameters` fields based on the context and the chosen template.
            5. Provide potential risks associated with the suggested actions.
            6. Describe the expected outcome if the remediation is successful.
            7. Estimate your confidence (0.0-1.0) in these suggestions.

            ## Required JSON Output Format:
            Your entire response MUST be a JSON object matching the `RemediationSteps` model.
            The `steps` field MUST be a list of objects, where EACH object is a `RemediationAction` model.
            Do NOT provide steps as simple strings.

            ```json
            // RemediationSteps Model Structure
            {{
              "steps": [
                {{ // RemediationAction Model Structure
                  "action_type": "string", // Must be one of the Available Remediation Command Templates
                  "resource_type": "string", // e.g., "pod", "deployment", "node"
                  "resource_name": "string", // Name of the resource
                  "namespace": "string | null", // Namespace (if applicable)
                  "parameters": {{ // Dictionary matching the requirements of the chosen action_type
                    // parameter_name: value
                  }}
                }}
                // ... more RemediationAction objects
              ],
              "risks": ["string"],
              "expected_outcome": "string",
              "confidence": "float" // Overall confidence
            }}
            ```

            ## Example (if the issue was a pod CrashLoopBackOff):
            ```json
            {{
              "steps": [
                {{
                  "action_type": "delete_pod",
                  "resource_type": "pod",
                  "resource_name": "my-pod-name",
                  "namespace": "default",
                  "parameters": {{}}, // delete_pod needs no extra parameters in its template
                  "confidence": 0.9
                }},
                {{
                  "action_type": "restart_deployment",
                  "resource_type": "deployment",
                  "resource_name": "my-deployment-name",
                  "namespace": "default",
                  "parameters": {{}},
                  "confidence": 0.7
                }}
              ],
              "risks": [
                "Deleting a pod will cause a brief interruption.",
                "Scaling up may consume more resources."
              ],
              "expected_outcome": "The problematic pod should be recreated and become healthy.",
              "confidence": 0.85
            }}
            ```
            Now, provide the JSON response for the given context:
            """

            # Implement robust retry logic for API calls
            max_retries = 3
            retry_delay = 2  # seconds

            for attempt in range(max_retries):
                try:
                    # Call Gemini with appropriate config for the JSON generation task
                    response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        contents=prompt,
                        generation_config=genai.types.GenerateContentConfig(
                            temperature=0.2,
                            top_p=0.9,
                            top_k=64,
                            response_mime_type="application/json",
                            candidate_count=1,
                            max_output_tokens=4096,
                        ),
                    )
                    
                    if response and hasattr(response, "text") and response.text:
                        break
                    else:
                        logger.warning(f"Empty or invalid response from Gemini API on attempt {attempt+1}")
                        if attempt < max_retries - 1:
                            logger.info(f"Retrying in {retry_delay} seconds...")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff
                except Exception as api_error:
                    logger.warning(f"API error on attempt {attempt+1}: {str(api_error)}")
                    if attempt < max_retries - 1:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff

            # Check if we have a response after retries
            if not response or not hasattr(response, "text"):
                logger.error("All API attempts failed, returning fallback response")
                return fallback_response

            response_text = response.text
            if not response_text or response_text.strip() == "":
                logger.error("Empty response text from Gemini API")
                return fallback_response

            # Enhanced JSON extraction with multiple fallback mechanisms
            try:
                # First, try to parse as direct JSON
                try:
                    result = json.loads(response_text)
                    logger.debug("Successfully parsed response as direct JSON")
                    
                    # Validate the structure matches what we need
                    if not self._validate_remediation_response(result):
                        logger.warning("Response JSON didn't validate, attempting fixes")
                        # Try to add missing fields or fix structure issues
                        if "steps" not in result or not isinstance(result["steps"], list):
                            result["steps"] = []
                        if "risks" not in result or not isinstance(result["risks"], list):
                            result["risks"] = ["Potential service disruption during remediation"]
                        if "expected_outcome" not in result:
                            result["expected_outcome"] = "Resolution of the identified issue"
                        if "confidence" not in result or not isinstance(result["confidence"], (int, float)):
                            result["confidence"] = 0.5
                    
                    return result
                
                except json.JSONDecodeError:
                    logger.warning("Could not parse direct JSON, trying to extract JSON from text")
                    
                    # Try to extract JSON from code blocks
                    json_pattern = r'```(?:json)?\s*([\s\S]*?)```'
                    matches = re.findall(json_pattern, response_text)
                    
                    for match in matches:
                        try:
                            extracted_json = json.loads(match)
                            if self._validate_remediation_response(extracted_json):
                                logger.debug("Successfully extracted valid JSON from code block")
                                return extracted_json
                        except json.JSONDecodeError:
                            continue
                    
                    # Try to find anything that looks like a JSON object in the text
                    object_pattern = r'\{\s*"[^"]+"\s*:'
                    obj_matches = re.findall(object_pattern, response_text)
                    if obj_matches:
                        # Find the start of the first match
                        start_idx = response_text.find(obj_matches[0])
                        # Try to parse from there to the end
                        try:
                            from_obj_start = response_text[start_idx:]
                            # Count braces to find the end of the object
                            brace_count = 0
                            end_idx = 0
                            for i, char in enumerate(from_obj_start):
                                if char == '{':
                                    brace_count += 1
                                elif char == '}':
                                    brace_count -= 1
                                    if brace_count == 0:
                                        end_idx = i + 1
                                        break
                            
                            if end_idx > 0:
                                json_str = from_obj_start[:end_idx]
                                extracted_json = json.loads(json_str)
                                if self._validate_remediation_response(extracted_json):
                                    logger.debug("Successfully extracted valid JSON from object pattern")
                                    return extracted_json
                        except Exception:
                            pass
                    
                    logger.warning("Could not find valid JSON in response, trying to extract structured data")
                    # As a last resort, try to parse the text to find actions, risks, etc.
                    parsed_remediation = self._parse_remediation_text(response_text)
                    return parsed_remediation
            
            except Exception as e:
                # Log the error but provide a fallback response
                logger.error(f"Error parsing remediation response: {str(e)}", exc_info=True)
                return fallback_response

        except Exception as e:
            # Log any errors but never crash
            logger.error(f"Error generating remediation: {str(e)}", exc_info=True)
            return {**fallback_response, "error": f"Unexpected error: {str(e)}"}

    async def verify_remediation_success(
        self,
        anomaly_analysis: dict,
        remediation_steps: dict,
        cluster_info: dict,
        metric_name: str,
        before_query_result: dict,
        after_query_result: dict,
    ) -> dict:
        """
        Use AI to verify if a remediation attempt was successful by comparing before and after states.

        Args:
            anomaly_analysis: Analysis of the original anomaly
            remediation_steps: Steps that were performed to remediate the issue
            cluster_info: Information about the Kubernetes cluster
            metric_name: Name of the primary metric being monitored
            before_query_result: State of resources/metrics before remediation
            after_query_result: State of resources/metrics after remediation

        Returns:
            Dictionary containing verification results with success status, reasoning, and confidence
        """
        try:
            if not self.model or not self.enabled:
                logger.warning("Gemini service not initialized or disabled")
                return self._create_minimal_verification_response(False)

            # Format the verification prompt with all the context
            prompt = f"""
            # Kubernetes Remediation Verification Request

            ## Original Anomaly:
            ```json
            {json.dumps(anomaly_analysis, default=str, indent=2)}
            ```

            ## Remediation Steps Performed:
            ```json
            {json.dumps(remediation_steps, default=str, indent=2)}
            ```

            ## Cluster Information:
            ```json
            {json.dumps(cluster_info, default=str, indent=2)}
            ```

            ## Primary Metric:
            {metric_name}

            ## Before Remediation State:
            ```json
            {json.dumps(before_query_result, default=str, indent=2)}
            ```

            ## After Remediation State:
            ```json
            {json.dumps(after_query_result, default=str, indent=2)}
            ```

            ## Verification Task:
            1. Compare the before and after states to determine if the remediation was successful
            2. Look for evidence of improvement in the primary metric
            3. Check for resolution of the original anomaly
            4. Identify any outstanding issues that might still need attention
            5. Consider whether the root cause (from the anomaly analysis) has been addressed

            Provide your verification assessment as a structured response with:
            - Success status (true/false)
            - Detailed reasoning for your assessment
            - Confidence score (0.0-1.0) in your verification
            """

            # Use structured output capability for more reliable response
            response = await asyncio.to_thread(
                lambda: self.client.models.generate_content(
                    model=self.model_type,
                    contents=prompt,
                    config={
                        'response_mime_type': 'application/json',
                        'response_schema': VerificationResult,
                    }
                )
            )

            # Process the structured response
            if response and hasattr(response, "parsed"):
                parsed_response = response.parsed
                if parsed_response:
                    logger.info(
                        f"Successfully verified remediation with confidence: {parsed_response.confidence}"
                    )
                    return parsed_response.model_dump()

            # Fall back to text extraction if structured output fails
            if response and hasattr(response, "text"):
                logger.warning(
                    "Structured verification failed, extracting from text response"
                )
                verification_result = self._extract_json_from_text(
                    response.text, VerificationResult
                )
                if verification_result:
                    return verification_result

            # Return a minimal valid response as last resort
            logger.error("Failed to extract verification result, using minimal response")
            return self._create_minimal_verification_response(False)

        except Exception as e:
            logger.error(f"Error in remediation verification: {str(e)}", exc_info=True)
            return self._create_minimal_verification_response(False)

    def _create_minimal_verification_response(self, success: bool) -> dict:
        """Create a minimal valid verification response.

        Args:
            success: Whether the verification succeeded

        Returns:
            Dictionary with minimal verification result
        """
        return {
            "success": success,
            "reasoning": "Automated verification could not be completed"
            if not success
            else "Verification succeeded",
            "confidence": 0.0 if not success else 0.5,
        }

    async def batch_process_anomaly(
        self, anomalies: List[Dict[str, Any]], cluster_info: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Process multiple anomalies in batch to improve efficiency.

        Args:
            anomalies: List of anomaly data dictionaries
            cluster_info: Information about the Kubernetes cluster

        Returns:
            List of processed anomalies with analysis and remediation
        """
        processed_anomalies = []

        for anomaly in anomalies:
            try:
                # Extract required information
                metric_name = anomaly.get("metric_name", "unknown_metric")
                query_result = anomaly.get("query_result", {})

                # Analyze the anomaly and safely handle the response
                raw_analysis = await self.analyze_anomaly(
                    anomaly_data=anomaly,
                    entity_info={
                        "entity_id": metric_name,
                        "entity_type": anomaly.get("entity_type", "unknown"),
                    },
                    related_metrics=anomaly.get("related_metrics", {}),
                    additional_context={"cluster_info": cluster_info},
                )

                # Safely handle the analysis response
                analysis = self._safely_handle_response(
                    response=raw_analysis,
                    output_model=AnomalyAnalysis,
                    default_return=self._create_minimal_response(
                        AnomalyAnalysis
                    ).model_dump(),
                )

                # Convert to AnomalyAnalysis object if it's a dict
                analysis_obj = analysis
                if isinstance(analysis, dict):
                    try:
                        # If there's an error key, don't try to create the object
                        if "error" not in analysis:
                            analysis_obj = AnomalyAnalysis(**analysis)
                    except Exception as e:
                        logger.warning(
                            f"Could not convert analysis dict to object: {e}"
                        )

                # Only generate remediation if severity is medium or higher
                remediation = None
                severity = (
                    analysis.get("severity")
                    if isinstance(analysis, dict)
                    else getattr(analysis_obj, "severity", "low")
                )

                if severity in ["medium", "high", "critical"]:
                    raw_remediation = await self.suggest_remediation(
                        anomaly_context=analysis_obj
                        if not isinstance(analysis_obj, dict)
                        else analysis,
                        failure_context=anomaly.get("failure_context", {}),
                    )

                    # Safely handle the remediation response
                    remediation = self._safely_handle_response(
                        response=raw_remediation,
                        output_model=RemediationSteps,
                        default_return={
                            "steps": ["No remediation steps could be generated"],
                            "risks": ["Unknown risks"],
                            "expected_outcome": "Unknown",
                            "confidence": 0.0,
                        },
                    )

                # Add to processed results
                processed_anomalies.append(
                    {
                        "original_data": anomaly,
                        "analysis": analysis
                        if isinstance(analysis, dict)
                        else analysis_obj.model_dump(),
                        "remediation": remediation,
                    }
                )

                logger.info(f"Successfully processed anomaly for {metric_name}")

            except Exception as e:
                logger.error(f"Error processing anomaly: {e}", exc_info=True)
                # Add the original anomaly with error information
                processed_anomalies.append(
                    {
                        "original_data": anomaly,
                        "error": str(e),
                        "analysis": None,
                        "remediation": None,
                    }
                )

        return processed_anomalies

    async def analyze_metrics(self, metrics_context: Dict[str, Any]) -> MetricInsights:
        """Analyze metric data and provide insights.

        Args:
            metrics_context: Dictionary containing metric data and context.

        Returns:
            MetricInsights object with analysis and insights.
        """
        logger.debug(
            f"Analyzing metrics: {metrics_context.get('metric_name', 'unknown')}"
        )

        if not self.model or not self.enabled:
            return self._create_minimal_response(MetricInsights)

        try:
            # Format the metric data for the prompt
            prompt = self._format_metrics_prompt(metrics_context)

            # Add system instruction

            # Call Gemini with function calling to get structured response
            return await self._call_gemini_with_model(
                prompt, MetricInsights, ModelType.FLASH
            )

        except Exception as e:
            logger.error(f"Error analyzing metrics: {e}", exc_info=True)
            return self._create_minimal_response(MetricInsights)

    async def get_remediation_steps(
        self, anomaly_analysis: AnomalyAnalysis, entity_info: Dict[str, Any]
    ) -> RemediationSteps:
        """Get recommended remediation steps for an anomaly.

        Args:
            anomaly_analysis: The analysis of the anomaly.
            entity_info: Information about the entity with the anomaly.

        Returns:
            RemediationSteps object with recommended actions.
        """
        logger.debug(
            f"Getting remediation for {entity_info.get('entity_id', 'unknown')}"
        )

        if not self.model or not self.enabled:
            return self._create_minimal_response(RemediationSteps)

        try:
            # Format the prompt for remediation
            prompt = self._format_remediation_prompt(anomaly_analysis, entity_info)

            # Add system instruction

            # Call Gemini with function calling to get structured response
            return await self._call_gemini_with_model(
                prompt, RemediationSteps, ModelType.FLASH
            )

        except Exception as e:
            logger.error(f"Error getting remediation steps: {e}", exc_info=True)
            return self._create_minimal_response(RemediationSteps)

    async def generate_prometheus_query(
        self, query_context: Dict[str, Any]
    ) -> PrometheusQueryResponse:
        """Generate an optimized Prometheus query based on the provided context.

        Args:
            query_context: Dictionary containing query generation context.

        Returns:
            PrometheusQueryResponse with the generated query and explanation.
        """
        logger.debug(
            f"Generating Prometheus query for: {query_context.get('purpose', 'unknown')}"
        )

        if not self.model or not self.enabled:
            return self._create_minimal_response(PrometheusQueryResponse)

        try:
            # Format the prompt for query generation
            prompt = self._format_query_generation_prompt(query_context)

            # Add system instruction

            # Call Gemini with function calling to get structured response
            return await self._call_gemini_with_model(
                prompt, PrometheusQueryResponse, ModelType.PRO
            )

        except Exception as e:
            logger.error(f"Error generating Prometheus query: {e}", exc_info=True)
            return self._create_minimal_response(PrometheusQueryResponse)

    async def verify_prometheus_query(
        self, query_verification_context: Dict[str, Any]
    ) -> QueryVerificationResponse:
        """Verify a Prometheus query and suggest improvements if needed.

        Args:
            query_verification_context: Dictionary containing the query and verification context.

        Returns:
            QueryVerificationResponse with verification results and suggestions.
        """
        logger.debug(
            f"Verifying Prometheus query: {query_verification_context.get('query', 'unknown')}"
        )

        if not self.model or not self.enabled:
            return self._create_minimal_response(QueryVerificationResponse)

        try:
            # Format the prompt for query verification
            prompt = self._format_query_verification_prompt(query_verification_context)

            # Add system instruction

            # Call Gemini with function calling to get structured response
            return await self._call_gemini_with_model(
                prompt, QueryVerificationResponse, ModelType.FLASH
            )

        except Exception as e:
            logger.error(f"Error verifying Prometheus query: {e}", exc_info=True)
            return self._create_minimal_response(QueryVerificationResponse)

    def get_smart_remediation(self, context: Dict[str, Any]) -> RemediationSteps:
        """Generate smart remediation steps using Gemini.

        Args:
            context: Dictionary containing remediation context including entity information,
                   anomaly details, available resources, and cluster context

        Returns:
            RemediationSteps object with recommended remediation steps
        """
        if not self.model or not self.enabled:
            raise ValueError("Gemini service not initialized")

        try:
            # Format the prompt using helper function
            prompt = self._format_smart_remediation_prompt(context)

            # System instruction for remediation
            system_instruction = """You are an expert Kubernetes SRE with deep knowledge of Kubernetes internals, \
container orchestration, and system troubleshooting. Your task is to generate precise remediation steps \
for Kubernetes issues using kubectl commands or YAML manifests. Focus on practical, executable solutions \
that follow best practices for Kubernetes operations. Always consider the potential impact of your \
recommended actions on system stability and application availability."""

            # Create async function to call model and run it
            async def call_model():
                return await self._call_gemini_with_model(
                    prompt,
                    RemediationSteps,
                    model_type=ModelType.PRO,
                    system_instruction=system_instruction,
                    temperature=0.2,
                    top_k=40,
                    top_p=0.95,
                    max_output_tokens=2048,
                )

            # Run the async function in the event loop
            response = asyncio.run(call_model())

            return response

        except Exception as e:
            logger.error(f"Error in smart remediation generation: {str(e)}")
            # Return fallback remediation steps
            return RemediationSteps(
                steps=[
                    "Unable to generate specific remediation steps due to an error.",
                    "Please check system logs and Kubernetes status manually.",
                ],
                commands=[],
                notes="Error occurred during remediation generation. Please consult with a Kubernetes administrator.",
            )

    def _format_metrics_prompt(
        self, metric_name: str, metric_data: Dict[str, Any], entity_info: Dict[str, Any]
    ) -> str:
        """Format the prompt for metric analysis.

        Args:
            metric_name: Name of the metric to analyze
            metric_data: Dictionary containing metric values and timeframes
            entity_info: Dictionary containing entity details

        Returns:
            Formatted prompt string
        """
        # Extract metric details
        metric_values = metric_data.get("values", [])
        metric_timestamps = metric_data.get("timestamps", [])

        # Format metric data for display
        metric_data_str = ""
        for i in range(min(len(metric_values), len(metric_timestamps))):
            metric_data_str += f"{metric_timestamps[i]}: {metric_values[i]}\n"

        # Limit data points if too many
        if len(metric_values) > 20:
            sample_indices = list(
                range(0, len(metric_values), len(metric_values) // 20)
            )
            sample_data_str = ""
            for idx in sample_indices:
                if idx < len(metric_values) and idx < len(metric_timestamps):
                    sample_data_str += (
                        f"{metric_timestamps[idx]}: {metric_values[idx]}\n"
                    )
            metric_data_str = sample_data_str

        # Extract entity details
        entity_id = entity_info.get("id", "unknown")
        entity_type = entity_info.get("type", "unknown")
        namespace = entity_info.get("namespace", "default")

        # Build the prompt
        prompt = f"""
        # Kubernetes Metric Analysis Request

        ## Metric Information:
        - Name: {metric_name}
        - Entity ID: {entity_id}
        - Entity Type: {entity_type}
        - Namespace: {namespace}

        ## Metric Data:
        ```
        {metric_data_str}
        ```

        ## Analysis Tasks:
        1. Analyze the pattern in the metric data
        2. Identify any anomalies or concerning trends
        3. Interpret what this means for the Kubernetes {entity_type}
        4. Provide possible explanations for any unusual patterns
        5. Suggest what actions might be appropriate

        Please provide a detailed analysis focusing on operational implications.
        """

        return prompt

    def _format_remediation_prompt(
        self, anomaly_analysis: Dict[str, Any], entity_info: Dict[str, Any]
    ) -> str:
        """Format the prompt for remediation generation.

        Args:
            anomaly_analysis: Dictionary containing anomaly analysis
            entity_info: Dictionary containing entity details

        Returns:
            Formatted prompt string
        """
        # Extract anomaly details
        root_cause = anomaly_analysis.get("root_cause", "Unknown")
        impact = anomaly_analysis.get("impact", "Unknown")
        severity = anomaly_analysis.get("severity", "medium")
        confidence = anomaly_analysis.get("confidence", 0.5)

        # Extract entity details
        entity_id = entity_info.get("id", "unknown")
        entity_type = entity_info.get("type", "unknown")
        namespace = entity_info.get("namespace", "default")

        # Build the prompt
        prompt = f"""
        # Kubernetes Remediation Request

        ## Anomaly Information:
        - Root Cause: {root_cause}
        - Impact: {impact}
        - Severity: {severity}
        - Confidence: {confidence}

        ## Entity Information:
        - ID: {entity_id}
        - Type: {entity_type}
        - Namespace: {namespace}

        ## Remediation Requirements:
        1. Provide specific steps to address the root cause
        2. Consider immediate actions to mitigate the impact
        3. Suggest preventive measures for future occurrences
        4. Prioritize steps based on severity and effectiveness
        5. Include command examples when applicable

        Please provide a comprehensive remediation plan with clear, actionable steps.
        """

        return prompt

    def _format_query_generation_prompt(
        self,
        purpose: str,
        metric_info: Dict[str, Any],
        filters: Dict[str, Any],
        timeframe: str,
    ) -> str:
        """Format the prompt for Prometheus query generation.

        Args:
            purpose: The purpose of the query
            metric_info: Dictionary containing metric information
            filters: Dictionary containing filter criteria
            timeframe: String describing the timeframe for the query

        Returns:
            Formatted prompt string
        """
        # Extract metric information
        metric_name = metric_info.get("name", "unknown")
        metric_type = metric_info.get("type", "unknown")
        metric_description = metric_info.get("description", "No description available")

        # Format filters for display
        filters_str = ""
        for key, value in filters.items():
            filters_str += f"- {key}: {value}\n"

        # Build the prompt
        prompt = f"""
        # Prometheus Query Generation Request

        ## Query Purpose:
        {purpose}

        ## Metric Information:
        - Name: {metric_name}
        - Type: {metric_type}
        - Description: {metric_description}

        ## Filter Criteria:
        {filters_str if filters_str else "No specific filters provided"}

        ## Timeframe:
        {timeframe}

        ## Query Requirements:
        1. Generate a precise PromQL query that meets the stated purpose
        2. Incorporate the specified filters appropriately
        3. Apply the correct aggregation method for the metric type
        4. Include appropriate rate/increase functions if needed for counter metrics
        5. Apply the specified timeframe correctly
        6. Ensure the query is optimized for performance

        Please provide a valid PromQL query with a brief explanation of how it works.
        """

        return prompt

    def _format_query_verification_prompt(
        self,
        query: str,
        purpose: str,
        available_metrics: List[str],
        sample_data: Dict[str, Any],
    ) -> str:
        """Format the prompt for Prometheus query verification.

        Args:
            query: The Prometheus query to verify
            purpose: The purpose of the query
            available_metrics: List of available metrics
            sample_data: Sample data for verification

        Returns:
            Formatted prompt string
        """
        # Format available metrics for display
        metrics_str = "\n".join([f"- {m}" for m in available_metrics[:20]])
        if len(available_metrics) > 20:
            metrics_str += (
                f"\n- ... {len(available_metrics) - 20} more metrics available"
            )

        # Format sample data for display
        sample_data_str = (
            json.dumps(sample_data, indent=2)
            if sample_data
            else "No sample data provided"
        )

        # Build the prompt
        prompt = f"""
        # Prometheus Query Verification Request

        ## Query to Verify:
        ```
        {query}
        ```

        ## Query Purpose:
        {purpose}

        ## Available Metrics (sample):
        {metrics_str}

        ## Sample Data:
        ```json
        {sample_data_str}
        ```

        ## Verification Tasks:
        1. Verify that the query is syntactically correct
        2. Check if the query uses metrics that are available
        3. Confirm that the query will fulfill its stated purpose
        4. Identify any potential performance issues
        5. Suggest improvements if needed

        Please provide a detailed verification analysis and corrected query if necessary.
        """

        return prompt

    def _format_smart_remediation_prompt(self, context: Dict[str, Any]) -> str:
        """Format the prompt for smart remediation generation.

        Args:
            context: Dictionary containing remediation context including entity information,
                   anomaly details, available resources, and cluster context

        Returns:
            Formatted prompt string
        """
        # Extract entity information
        entity_info = context.get("entity_info", {})
        entity_type = entity_info.get("type", "unknown")
        entity_id = entity_info.get("id", "unknown")
        namespace = entity_info.get("namespace", "default")

        # Extract anomaly details
        anomaly = context.get("anomaly", {})
        metric_name = anomaly.get("metric_name", "unknown")
        root_cause = anomaly.get("root_cause", "Unknown")
        severity = anomaly.get("severity", "medium")

        # Extract available resources
        resources = context.get("resources", {})
        resource_str = (
            json.dumps(resources, indent=2)
            if resources
            else "No resource information available"
        )

        # Extract cluster context if available
        cluster_context = context.get("cluster_context", {})
        context_str = (
            json.dumps(cluster_context, indent=2)
            if cluster_context
            else "No additional cluster context available"
        )

        # Build the prompt
        prompt = f"""
        # Kubernetes Smart Remediation Request

        ## Entity Information:
        - Type: {entity_type}
        - ID: {entity_id}
        - Namespace: {namespace}

        ## Anomaly Details:
        - Metric: {metric_name}
        - Root Cause: {root_cause}
        - Severity: {severity}

        ## Available Resources:
        ```json
        {resource_str}
        ```

        ## Cluster Context:
        ```json
        {context_str}
        ```

        ## Remediation Requirements:
        1. Generate specific kubectl commands or YAML manifests to remediate the issue
        2. Prioritize actions based on severity and effectiveness
        3. Consider both immediate fixes and long-term solutions
        4. Include verification steps to confirm the remediation was successful
        5. Add safety precautions where necessary (e.g., adding --dry-run first)
        6. Consider potential impact on other services

        Please provide a comprehensive remediation plan with specific, executable commands.
        """

        return prompt

    async def verify_remediation(self, verification_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Verify if remediation steps were successful by examining the current state.

        Args:
            verification_context: Dictionary with entity info, remediation details,
                                 original issue, and current state

        Returns:
            Dictionary with verification result including success status, reasoning, and confidence
        """
        logger.debug(f"Verifying remediation for {verification_context.get('entity', {}).get('entity_id', 'unknown')}")

        if not self.enabled or not self.client:
            logger.error("Gemini service not initialized.")
            return {
                "success": False,
                "reasoning": "Verification failed: Gemini service is not available",
                "confidence": 0.0
            }

        try:
            # Format the context for the prompt
            entity = verification_context.get("entity", {})
            entity_id = entity.get("entity_id", "unknown")
            entity_type = entity.get("entity_type", "unknown")

            remediation = verification_context.get("remediation", {})
            action = remediation.get("action", "unknown")

            original_issue = verification_context.get("original_issue", {})
            current_state = verification_context.get("current_state", {})

            # Build the prompt
            prompt = f"""
            # Kubernetes Remediation Verification Request

            ## Entity Details:
            - Type: {entity_type}
            - ID: {entity_id}
            - Namespace: {entity.get('namespace', 'default')}

            ## Remediation Performed:
            - Action: {action}
            - Output: {remediation.get('output', 'No output available')}
            - Status: {remediation.get('status', 'unknown')}

            ## Original Issue:
            - Status: {original_issue.get('status', 'unknown')}
            - Anomaly Score: {original_issue.get('anomaly_score', 0)}
            - Notes: {original_issue.get('notes', 'No notes available')}

            ## Current State:
            - Resource Exists: {current_state.get('resource_exists', 'unknown')}
            - Current Issues: {current_state.get('issues_detected', [])}
            - Current Status: {json.dumps(current_state.get('k8s_status', {}), default=str)[:500]}

            ## Verification Task:
            Determine if the remediation action resolved the original issue by comparing the original issue
            with the current state. Consider whether:
            1. The original symptoms are gone
            2. The resource is in a healthy state
            3. No new issues have been introduced
            4. The action performed matches what was needed for the reported problem

            Provide your verification assessment as a structured response in JSON format with these fields:
            - success: boolean indicating if remediation was successful
            - reasoning: detailed explanation of your assessment
            - confidence: your confidence score (0.0-1.0) in this assessment
            """

            # Use the Flash model for faster response on this task
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=ModelType.FLASH_LITE.value,
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                config=genai.types.GenerateContentConfig(
                    temperature=0.2,
                    top_p=0.95,
                    top_k=64,
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                ),
            )

            # Process the response text
            if not response or not hasattr(response, "text"):
                logger.warning("Received empty response from Gemini API")
                return {
                    "success": False,
                    "reasoning": "Verification failed: Unable to get assessment from Gemini",
                    "confidence": 0.0
                }

            # Try to extract JSON from the response text
            try:
                # First try to extract JSON from code blocks
                matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", response.text, re.DOTALL)

                if matches:
                    # Take the first match that parses as valid JSON
                    for match in matches:
                        try:
                            result = json.loads(match)
                            if set(result.keys()) >= {"success", "reasoning", "confidence"}:
                                return result
                        except json.JSONDecodeError:
                            continue

                # If no valid JSON in code blocks, try the whole response
                try:
                    result = json.loads(response.text)
                    if set(result.keys()) >= {"success", "reasoning", "confidence"}:
                        return result
                except json.JSONDecodeError:
                    pass

                # If we still don't have a valid response, extract info manually
                return self._parse_verification_result(response.text)

            except Exception as e:
                logger.warning(f"Error extracting verification results from response: {e}")
                return {
                    "success": False,
                    "reasoning": f"Verification processing error: {str(e)}",
                    "confidence": 0.0
                }

        except Exception as e:
            logger.error(f"Error in remediation verification: {str(e)}", exc_info=True)
            return {
                "success": False,
                "reasoning": f"Verification error: {str(e)}",
                "confidence": 0.0
            }

    def _parse_verification_result(self, text: str) -> Dict[str, Any]:
        """
        Parse verification result from unstructured text when JSON extraction fails.

        Args:
            text: Response text from the model

        Returns:
            Dictionary with parsed verification results
        """
        result = {
            "success": False,
            "reasoning": "Could not determine verification status from response",
            "confidence": 0.0
        }

        # Look for success indicators
        success_indicators = [
            "remediation was successful",
            "issue has been resolved",
            "successfully resolved",
            "action was effective",
            "problem has been fixed"
        ]

        failure_indicators = [
            "remediation was unsuccessful",
            "issue persists",
            "not successfully resolved",
            "action was ineffective",
            "problem remains"
        ]

        # Check for success or failure patterns
        text_lower = text.lower()
        for indicator in success_indicators:
            if indicator in text_lower:
                result["success"] = True
                break

        for indicator in failure_indicators:
            if indicator in text_lower:
                result["success"] = False
                break

        # Try to extract reasoning - look for explanation sections
        reasoning_sections = re.findall(r"(?:reasoning|explanation|assessment|analysis):\s*(.*?)(?:\n\n|\n#|\Z)",
                                       text, re.IGNORECASE | re.DOTALL)
        if reasoning_sections:
            result["reasoning"] = reasoning_sections[0].strip()
        else:
            # If no section headers, use the whole text but limit size
            result["reasoning"] = text[:500] + ("..." if len(text) > 500 else "")

        # Try to extract confidence - look for numbers
        confidence_matches = re.findall(r"(?:confidence|certainty):\s*(0\.\d+|1\.0)", text, re.IGNORECASE)
        if confidence_matches:
            try:
                result["confidence"] = float(confidence_matches[0])
            except ValueError:
                pass

        return result

    def _validate_remediation_response(self, response: Dict[str, Any]) -> bool:
        """
        Validate if a remediation response has the required fields and structure.

        Args:
            response: The response dictionary to validate

        Returns:
            Boolean indicating if the response is valid
        """
        required_fields = ["steps", "expected_outcome", "confidence"]

        # Check that all required fields are present
        for field in required_fields:
            if field not in response:
                return False

        # Check specific field types
        if not isinstance(response["steps"], list):
            return False

        if not isinstance(response["expected_outcome"], str):
            return False

        if not isinstance(response["confidence"], (int, float)):
            return False

        # Ensure steps is not empty
        if len(response["steps"]) == 0:
            return False

        # Add risks field if not present
        if "risks" not in response:
            response["risks"] = ["No specific risks identified"]
        elif not isinstance(response["risks"], list):
            response["risks"] = [str(response["risks"])]

        return True

    def _parse_remediation_text(self, text: str) -> Dict[str, Any]:
        """
        Parse remediation steps from unstructured text response when JSON extraction fails.

        Args:
            text: Response text from the model

        Returns:
            Dictionary with parsed remediation steps
        """
        result = {
            "steps": ["No specific remediation steps could be extracted"],
            "risks": ["Manual intervention may be required"],
            "expected_outcome": "Resolution of the identified issue",
            "confidence": 0.0
        }

        # Look for steps (numbered or bulleted lists)
        steps = []
        risks = []
        expected_outcome = ""

        # Try to extract steps section first
        steps_section_match = re.search(r"(?:steps|commands|remediation steps|actions)[:]\s*(.*?)(?:\n\n|#|\Z)",
                                       text, re.IGNORECASE | re.DOTALL)

        if steps_section_match:
            steps_text = steps_section_match.group(1)
            # Split into lines and look for numbered or bulleted items
            for line in steps_text.split('\n'):
                line = line.strip()
                if re.match(r"^\d+[\.)]", line) or line.startswith('-') or line.startswith('*'):
                    # Remove the bullet/number and add to steps
                    step = re.sub(r"^\d+[\.)]|^[-*]\s*", "", line).strip()
                    if step:
                        steps.append(step)

        # If no structured steps found, try to extract kubectl commands
        if not steps:
            kubectl_commands = re.findall(r"kubectl\s+\w+\s+[\w\-\./]+(?:\s+(?:--\w+(?:=[\w\-\./:]+)?|\w+))*",
                                         text, re.IGNORECASE)
            if kubectl_commands:
                steps = kubectl_commands

        # If still no steps, try to find any commands that look like shell commands
        if not steps:
            potential_commands = re.findall(r"^(?!#)[a-z]+(?:\s+(?:-{1,2}[a-z\-]+(?:=\S+)?|\S+))+",
                                          text, re.MULTILINE | re.IGNORECASE)
            if potential_commands:
                steps = [cmd.strip() for cmd in potential_commands if len(cmd) > 10]  # Longer commands only

        # Extract risks
        risks_section_match = re.search(r"(?:risks|potential issues|considerations)[:]\s*(.*?)(?:\n\n|#|\Z)",
                                       text, re.IGNORECASE | re.DOTALL)

        if risks_section_match:
            risks_text = risks_section_match.group(1)
            # Split into lines and look for numbered or bulleted items
            for line in risks_text.split('\n'):
                line = line.strip()
                if re.match(r"^\d+[\.)]", line) or line.startswith('-') or line.startswith('*'):
                    # Remove the bullet/number and add to risks
                    risk = re.sub(r"^\d+[\.)]|^[-*]\s*", "", line).strip()
                    if risk:
                        risks.append(risk)

        # If no structured risks found, try to extract sentences containing risk keywords
        if not risks:
            risk_sentences = re.findall(r"[^.!?]*(?:risk|caution|careful|warning|impact)[^.!?]*[.!?]",
                                      text, re.IGNORECASE)
            if risk_sentences:
                risks = [s.strip() for s in risk_sentences]

        # Extract expected outcome
        outcome_match = re.search(r"(?:expected outcome|result|expected result|outcome)[:]\s*(.*?)(?:\n\n|#|\Z)",
                                 text, re.IGNORECASE | re.DOTALL)

        if outcome_match:
            expected_outcome = outcome_match.group(1).strip()

        # Try to extract confidence
        confidence_match = re.search(r"(?:confidence|certainty)[:]\s*(0\.\d+|1\.0)",
                                    text, re.IGNORECASE)
        confidence = 0.0
        if confidence_match:
            try:
                confidence = float(confidence_match.group(1))
            except ValueError:
                pass

        # Update the result dictionary with extracted information
        if steps:
            result["steps"] = steps

        if risks:
            result["risks"] = risks

        if expected_outcome:
            result["expected_outcome"] = expected_outcome

        if confidence > 0:
            result["confidence"] = confidence

        return result

    async def create_context_cache(
        self,
        content: Union[str, Dict, List],
        ttl_seconds: int = 3600,
        system_instruction: Optional[str] = None,
        cache_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a context cache for efficient reuse of large context data.

        Args:
            content: The content to cache (text or structured data)
            ttl_seconds: Time-to-live for the cache in seconds (default 1 hour)
            system_instruction: Optional system instruction to include in the cache
            cache_name: Optional name for the cache (will be generated if not provided)

        Returns:
            Dictionary with cache details including name and expiry time
        """
        if not self.enabled or not self.client:
            logger.error("Gemini service not initialized")
            return {"error": "Gemini service not initialized"}

        try:
            # Format TTL as string in the format required by the API
            ttl = f"{ttl_seconds}s"

            # Generate a name if not provided
            if not cache_name:
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                cache_name = f"kubewise_cache_{timestamp}"

            # Prepare content (ensure it's properly formatted for the API)
            if isinstance(content, str):
                cache_content = content
            else:
                try:
                    cache_content = json.dumps(content, default=str)
                except Exception as e:
                    logger.error(f"Failed to serialize content for caching: {e}")
                    return {"error": f"Failed to serialize cache content: {str(e)}"}

            # Prepare cache config
            cache_config = {
                "ttl": ttl
            }

            if system_instruction:
                cache_config["system_instructions"] = [{"content": system_instruction}]

            # Create the cache
            logger.info(f"Creating context cache with TTL: {ttl}")
            cache = await asyncio.to_thread(
                lambda: self.client.caches.create(
                    name=cache_name,
                    contents=cache_content,
                    metadata=cache_config
                )
            )

            logger.info(f"Successfully created context cache: {cache.name}")
            return {
                "name": cache.name,
                "expire_time": cache.expire_time,
                "status": "created"
            }

        except Exception as e:
            logger.error(f"Error creating context cache: {e}", exc_info=True)
            return {"error": f"Failed to create context cache: {str(e)}"}

    async def use_cached_context(
        self,
        cache_name: str,
        query: str,
        output_model: Optional[Type[BaseModel]] = None,
        model_type: Optional[ModelType] = None
    ) -> Dict[str, Any]:
        """
        Generate content using a previously created context cache.

        Args:
            cache_name: The name of the cache to use
            query: The query to send with the cached context
            output_model: Optional Pydantic model for structured output
            model_type: Optional specific model to use

        Returns:
            Generated content using the cached context
        """
        if not self.enabled or not self.client:
            logger.error("Gemini service not initialized")
            return {"error": "Gemini service not initialized"}

        try:
            model_name = model_type.value if model_type else self.model_type

            # Create config with cached content reference
            config = genai.types.GenerateContentConfig(
                temperature=0.2,
                top_p=0.95,
                top_k=64,
                max_output_tokens=4096,
                cached_content=cache_name
            )

            # Add structured output config if a model is provided
            if output_model:
                config.response_mime_type = "application/json"
                config.response_schema = output_model

            # Generate content using the cache
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=model_name,
                contents=[{"role": "user", "parts": [{"text": query}]}],
                config=config
            )

            # Process response (similar to _call_gemini_with_model)
            if hasattr(response, "parsed") and response.parsed is not None:
                parsed = response.parsed
                return parsed.model_dump() if isinstance(parsed, BaseModel) else parsed

            # Fallback to text response
            if hasattr(response, "text"):
                if output_model:
                    return self._extract_json_from_text(response.text, output_model)
                else:
                    return {"response": response.text}

            logger.error("Invalid response format from Gemini API")
            return {"error": "Invalid response format from Gemini API"}

        except Exception as e:
            logger.error(f"Error using cached context: {e}", exc_info=True)
            return {"error": f"Error using cached context: {str(e)}"}

    async def list_context_caches(self) -> List[Dict[str, Any]]:
        """
        List all available context caches.

        Returns:
            List of cache information dictionaries
        """
        if not self.enabled or not self.client:
            logger.error("Gemini service not initialized")
            return [{"error": "Gemini service not initialized"}]

        try:
            caches = await asyncio.to_thread(
                lambda: self.client.caches.list()
            )

            cache_info = []
            for cache in caches:
                cache_info.append({
                    "name": cache.name,
                    "create_time": cache.create_time,
                    "expire_time": cache.expire_time,
                    "size_bytes": getattr(cache, "size_bytes", "unknown")
                })

            return cache_info

        except Exception as e:
            logger.error(f"Error listing context caches: {e}", exc_info=True)
            return [{"error": f"Error listing context caches: {str(e)}"}]

    async def delete_context_cache(self, cache_name: str) -> Dict[str, Any]:
        """
        Delete a context cache.

        Args:
            cache_name: Name of the cache to delete

        Returns:
            Status dictionary
        """
        if not self.enabled or not self.client:
            logger.error("Gemini service not initialized")
            return {"error": "Gemini service not initialized"}

        try:
            await asyncio.to_thread(
                lambda: self.client.caches.delete(name=cache_name)
            )

            logger.info(f"Successfully deleted context cache: {cache_name}")
            return {"status": "deleted", "name": cache_name}

        except Exception as e:
            logger.error(f"Error deleting context cache: {e}", exc_info=True)
            return {"error": f"Error deleting context cache: {str(e)}"}

    async def analyze_with_thinking(
        self,
        prompt: str,
        output_model: Optional[Type[BaseModel]] = None,
        thinking_budget: int = 8192,
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Analyze a problem using Gemini 2.5's advanced thinking capabilities.

        This method leverages Gemini 2.5's thinking process for improved reasoning and
        multi-step planning, which is beneficial for complex K8s root cause analysis.

        Args:
            prompt: The prompt to analyze
            output_model: Optional Pydantic model for structured output
            thinking_budget: Thinking token budget (0-24576, 0 disables thinking)
            system_instruction: Optional system instruction to guide thinking
            **kwargs: Additional keyword arguments for generation config

        Returns:
            Dictionary with analysis results
        """
        if not self.enabled or not self.client:
            logger.error("Gemini service not initialized")
            return {"error": "Gemini service not initialized"}

        try:
            # Use the PRO_THINKING model with appropriate config
            model = ModelType.PRO_THINKING.value

            # Create generation config with thinking budget
            config = genai.types.GenerateContentConfig(
                temperature=kwargs.get("temperature", 0.2),
                top_p=kwargs.get("top_p", 0.95),
                top_k=kwargs.get("top_k", 64),
                max_output_tokens=kwargs.get("max_output_tokens", 4096),
                thinking_budget=thinking_budget  # Add thinking budget for enhanced reasoning
            )

            # Add structured output if output model is provided
            if output_model:
                config.response_mime_type = "application/json"
                config.response_schema = output_model

            # Add tools if provided
            if "tools" in kwargs:
                config.tools = kwargs["tools"]

            # Create content
            contents = []

            # Add system instruction if provided
            if system_instruction:
                contents.append({
                    "role": "system",
                    "parts": [{"text": system_instruction}]
                })

            # Add user prompt
            contents.append({
                "role": "user",
                "parts": [{"text": prompt}]
            })

            # Implement robust retry logic
            max_retries = 3
            retry_delay = 2  # seconds

            for attempt in range(max_retries):
                try:
                    logger.info(f"Calling Gemini thinking model (attempt {attempt+1}/{max_retries})")

                    response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        model=model,
                        contents=contents,
                        config=config
                    )

                    # Process the response
                    if hasattr(response, "parsed") and response.parsed is not None:
                        # Use structured output if available
                        parsed_response = response.parsed
                        logger.info("Successfully obtained structured response from thinking model")
                        return parsed_response.model_dump() if isinstance(parsed_response, BaseModel) else parsed_response

                    # Fall back to text response
                    if hasattr(response, "text") and response.text.strip():
                        logger.info(f"Got text response from thinking model ({len(response.text)} chars)")
                        if output_model:
                            # Try to extract JSON if output model was provided
                            return self._extract_json_from_text(response.text, output_model)
                        else:
                            # Return raw text if no model was specified
                            return {"analysis": response.text.strip()}

                    # If neither parsed nor text response is available
                    logger.warning(f"Empty response from Gemini thinking model on attempt {attempt+1}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (2 ** attempt))  # Exponential backoff

                except Exception as e:
                    logger.error(f"Error on attempt {attempt+1}: {e}", exc_info=True)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (2 ** attempt))
                    else:
                        return {"error": f"Failed to get response after {max_retries} attempts: {str(e)}"}

            # If we got here, all attempts failed
            return {"error": "Failed to get a valid response from the thinking model"}

        except Exception as e:
            logger.error(f"Error in analyze_with_thinking: {e}", exc_info=True)
            return {"error": f"Failed to analyze with thinking capabilities: {str(e)}"}

    async def call_function(
        self,
        prompt: str,
        functions: List[Dict[str, Any]],
        function_mode: str = "AUTO",
        model_type: Optional[ModelType] = None,
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Call Gemini with function calling capability to get a structured function call response.

        Args:
            prompt: The user prompt to process
            functions: List of function definitions with name, description and parameters schema
            function_mode: Mode for function calling ('AUTO', 'ANY', 'NONE')
            model_type: Optional specific model to use
            system_instruction: Optional system instruction to guide the model

        Returns:
            Dictionary with function call details or text response
        """
        if not self.enabled or not self.client:
            logger.error("Gemini service not initialized")
            return {"error": "Gemini service not initialized"}

        try:
            # Use specified model or default
            model_name = model_type.value if model_type else self.model_type

            # Configure the function calling tool
            tool_config = {
                "function_calling_config": {
                    "mode": function_mode,  # AUTO, ANY, or NONE
                }
            }

            if function_mode == "ANY" and kwargs.get("allowed_functions"):
                tool_config["function_calling_config"]["allowed_function_names"] = kwargs.get("allowed_functions")

            # Create the tools configuration with the functions
            tools = [{"function_declarations": functions}]

            # Create configuration
            config = genai.types.GenerateContentConfig(
                temperature=kwargs.get("temperature", 0.2),
                top_p=kwargs.get("top_p", 0.95),
                top_k=kwargs.get("top_k", 64),
                max_output_tokens=kwargs.get("max_output_tokens", 2048),
                tools=tools,
                tool_config=tool_config
            )

            # Create content with system instruction if provided
            contents = []
            if system_instruction:
                contents.append({
                    "role": "system",
                    "parts": [{"text": system_instruction}]
                })

            # Add user prompt
            contents.append({
                "role": "user",
                "parts": [{"text": prompt}]
            })

            # Make the API call with error handling
            max_retries = 3
            retry_delay = 2  # seconds

            for attempt in range(max_retries):
                try:
                    logger.debug(f"Calling Gemini with function calling (attempt {attempt+1}/{max_retries})")

                    response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        model=model_name,
                        contents=contents,
                        config=config
                    )

                    # Process response for function calls
                    if (response and
                        hasattr(response, "candidates") and
                        response.candidates and
                        hasattr(response.candidates[0], "content") and
                        response.candidates[0].content and
                        hasattr(response.candidates[0].content, "parts")):

                        for part in response.candidates[0].content.parts:
                            # Check for function_call part
                            if hasattr(part, "function_call") and part.function_call:
                                function_call = part.function_call
                                logger.info(f"Received function call: {function_call.name}")

                                return {
                                    "function_call": True,
                                    "name": function_call.name,
                                    "arguments": function_call.args,
                                    "raw_arguments": json.dumps(function_call.args)
                                }

                    # If we didn't get a function call, return the text response
                    if hasattr(response, "text"):
                        return {
                            "function_call": False,
                            "text": response.text
                        }

                    # No valid response
                    logger.warning(f"No valid response received on attempt {attempt+1}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (2 ** attempt))

                except Exception as e:
                    logger.error(f"Error calling function on attempt {attempt+1}: {e}", exc_info=True)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (2 ** attempt))
                    else:
                        return {"error": f"Failed to call function after {max_retries} attempts: {str(e)}"}

            # If we get here, all attempts failed
            return {"error": "Failed to get a valid function call response"}

        except Exception as e:
            logger.error(f"Error in function calling: {e}", exc_info=True)
            return {"error": f"Function calling error: {str(e)}"}
