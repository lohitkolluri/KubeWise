from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class OperationResult(BaseModel):
    """Common response model for all operations."""

    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    errors: Optional[List[str]] = None
