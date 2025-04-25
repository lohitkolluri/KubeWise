import time
import uuid
from typing import Callable, Dict, Any, List

import traceback
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import logger as app_logger
from app.core.exceptions import (
    KubeWiseException, 
    ValidationError,
    ResourceNotFoundError,
    AuthorizationError,
    AuthenticationError,
    OperationFailedError,
    ConfigurationError,
    ServiceUnavailableError,
    ERROR_TO_HTTP_STATUS
)
from app.utils.prometheus_scraper import prometheus_scraper

class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Middleware that adds request context to each request.
    Includes request ID, timing information, and error handling.
    """
    
    def __init__(self, app: FastAPI):
        super().__init__(app)
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process the request and add context information.
        
        Args:
            request: The incoming request
            call_next: The next middleware or route handler
            
        Returns:
            The response
        """
        # Generate a request ID for tracking
        request_id = str(uuid.uuid4())
        
        # Add the request ID to the request state
        request.state.request_id = request_id
        
        # Record start time
        start_time = time.time()
        
        # Create context for structured logging
        log_context = {
            "request_id": request_id,
            "method": request.method,
            "url": str(request.url),
            "client_ip": request.client.host if request.client else "unknown"
        }
        
        # Log the request
        app_logger.info(
            f"Request started: {request.method} {request.url.path}",
            **log_context
        )
        
        try:
            # Process the request
            response = await call_next(request)
            
            # Calculate processing time
            process_time = time.time() - start_time
            
            # Add request ID and timing headers to response
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = str(process_time)
            
            # Log the successful response
            app_logger.info(
                f"Request completed: {request.method} {request.url.path}",
                status_code=response.status_code,
                process_time=process_time,
                **log_context
            )
            
            return response
            
        except KubeWiseException as e:
            # Handle our custom exceptions with proper status codes
            status_code = ERROR_TO_HTTP_STATUS.get(e.__class__, 500)
            
            app_logger.warning(
                f"Request error: {e.__class__.__name__}: {e.message}",
                exception=str(e),
                exception_type=e.__class__.__name__,
                status_code=status_code,
                **log_context,
                **e.details
            )
            
            return JSONResponse(
                status_code=status_code,
                content=e.to_dict()
            )
            
        except Exception as e:
            # Handle unexpected exceptions
            app_logger.error(
                f"Unhandled exception: {str(e)}",
                exception=str(e),
                exception_type=e.__class__.__name__,
                traceback=traceback.format_exc(),
                **log_context
            )
            
            return JSONResponse(
                status_code=500,
                content={
                    "error_type": "InternalServerError",
                    "message": "An unexpected error occurred.",
                    "details": {
                        "request_id": request_id
                    }
                }
            )

class PrometheusMiddleware(BaseHTTPMiddleware):
    """Middleware for Prometheus metrics collection and logging."""
    
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        method = request.method
        endpoint = request.url.path
        
        try:
            response = await call_next(request)
            status_code = response.status_code
            
            # Record request duration
            duration = time.time() - start_time
            
            # Update Prometheus metrics if the scraper is initialized
            if hasattr(prometheus_scraper, 'request_counter') and prometheus_scraper.request_counter:
                prometheus_scraper.request_counter.labels(method=method, endpoint=endpoint, status=status_code).inc()
            
            if hasattr(prometheus_scraper, 'request_duration') and prometheus_scraper.request_duration:
                prometheus_scraper.request_duration.labels(method=method, endpoint=endpoint).observe(duration)
                
            app_logger.debug(
                f"Metrics recorded",
                method=method,
                path=endpoint,
                duration=f"{duration:.3f}s",
                status_code=status_code
            )
            
            return response
            
        except Exception as exc:
            # Calculate duration even for failed requests
            duration = time.time() - start_time
            status_code = 500
            
            # Still record metrics for failed requests
            if hasattr(prometheus_scraper, 'request_counter') and prometheus_scraper.request_counter:
                prometheus_scraper.request_counter.labels(method=method, endpoint=endpoint, status=status_code).inc()
            
            if hasattr(prometheus_scraper, 'request_duration') and prometheus_scraper.request_duration:
                prometheus_scraper.request_duration.labels(method=method, endpoint=endpoint).observe(duration)
            
            # Log and re-raise
            app_logger.error(
                f"Request error in metrics collection",
                method=method,
                path=endpoint,
                duration=f"{duration:.3f}s",
                exception=str(exc),
                exception_type=exc.__class__.__name__
            )
            raise

def format_validation_errors(errors: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """
    Format validation errors into a more user-friendly structure.
    
    Args:
        errors: List of validation error dictionaries from RequestValidationError
        
    Returns:
        Dictionary mapping field paths to error messages
    """
    formatted_errors = {}
    
    for error in errors:
        # Get the location of the error (path in JSON object)
        loc = error.get("loc", [])
        field_path = ".".join(str(item) for item in loc if item != "body")
        
        # Get the error message
        msg = error.get("msg", "Unknown validation error")
        
        # Add to the formatted errors
        if field_path not in formatted_errors:
            formatted_errors[field_path] = []
            
        formatted_errors[field_path].append(msg)
        
    return formatted_errors

def create_exception_handlers():
    """Create exception handlers for different types of errors."""
    
    handlers = {}
    
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Handle validation errors from request data."""
        # Get request ID from state if available
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        
        # Format the validation errors
        formatted_errors = format_validation_errors(exc.errors())
        
        # Create a detailed error response
        error_detail = {
            "error_type": "ValidationError",
            "message": "Validation error in request data",
            "details": {
                "request_id": request_id,
                "validation_errors": formatted_errors
            }
        }
        
        # Log the validation error with details
        app_logger.warning(
            "Validation error in request",
            path=request.url.path,
            validation_errors=formatted_errors,
            request_id=request_id
        )
        
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_detail
        )
    
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Handle HTTP exceptions."""
        # Get request ID from state if available
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        
        # Create error detail
        error_detail = {
            "error_type": f"HTTPError_{exc.status_code}",
            "message": str(exc.detail),
            "details": {
                "request_id": request_id
            }
        }
        
        # Log the HTTP error
        app_logger.warning(
            f"HTTP exception: {exc.status_code}",
            detail=str(exc.detail),
            path=request.url.path,
            request_id=request_id
        )
        
        return JSONResponse(
            status_code=exc.status_code,
            content=error_detail
        )
        
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle all other exceptions globally, providing consistent error responses."""
        
        # Get the request ID from state if available
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        
        # Default status code and error structure
        status_code = 500
        error_detail = {
            "error_type": "InternalServerError",
            "message": "Internal server error",
            "details": {
                "request_id": request_id
            }
        }
        
        # For our custom KubeWiseException
        if isinstance(exc, KubeWiseException):
            # Use details from our custom exception
            error_detail["message"] = exc.message
            error_detail["error_type"] = exc.__class__.__name__
            error_detail["details"] = exc.details or {}
            error_detail["details"]["request_id"] = request_id
            
            # Use the defined mapping for status codes
            status_code = ERROR_TO_HTTP_STATUS.get(exc.__class__, 500)
        
        # For all other exceptions
        else:
            error_detail["message"] = str(exc)
            error_detail["error_type"] = exc.__class__.__name__
            
            # Include traceback in error details during development
            if app_logger.debug_mode:
                error_detail["details"]["traceback"] = traceback.format_exc().split('\n')
        
        # Log the error with our structured logger
        log_level = "warning" if status_code < 500 else "error"
        
        if log_level == "warning":
            app_logger.warning(
                f"Exception handled: {error_detail['error_type']}",
                status_code=status_code,
                path=request.url.path,
                message=error_detail['message'],
                exception_type=error_detail['error_type'],
                request_id=request_id
            )
        else:
            app_logger.error(
                f"Exception handled: {error_detail['error_type']}",
                status_code=status_code,
                path=request.url.path,
                message=error_detail['message'],
                exception_type=error_detail['error_type'],
                request_id=request_id,
                exception=str(exc),
                traceback=traceback.format_exc()
            )
        
        return JSONResponse(
            status_code=status_code,
            content=error_detail
        )
    
    # Add handlers for specific exception types
    handlers[RequestValidationError] = validation_exception_handler
    handlers[StarletteHTTPException] = http_exception_handler
    handlers[Exception] = global_exception_handler
    
    return handlers

def setup_middleware(app: FastAPI) -> None:
    """
    Configure middleware for the application.
    
    Args:
        app: The FastAPI application
    """
    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins
        allow_credentials=True,
        allow_methods=["*"],  # Allow all methods
        allow_headers=["*"],  # Allow all headers
    )
    
    # Add request context middleware
    app.add_middleware(RequestContextMiddleware)
    
    # Add Prometheus metrics middleware
    app.add_middleware(PrometheusMiddleware)
    
    # Add exception handlers
    exception_handlers = create_exception_handlers()
    for exception_type, handler in exception_handlers.items():
        app.add_exception_handler(exception_type, handler)
    
    # Log middleware setup
    app_logger.info("Middleware configured", middleware=["CORS", "RequestContext", "PrometheusMetrics", "ExceptionHandlers"])
