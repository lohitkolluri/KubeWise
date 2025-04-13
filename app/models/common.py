from pydantic import BaseModel
from typing import Optional, Dict, Any, List

class OperationResult(BaseModel):
    """Common response model for all operations."""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    errors: Optional[List[str]] = None
