"""
Base model for KubeWise models using Pydantic.
"""

from typing import Any, Optional

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, field_validator


# Simple function to convert ObjectId to string
def serialize_object_id(obj_id: Any) -> str:
    if isinstance(obj_id, ObjectId):
        return str(obj_id)
    return str(obj_id)


# Custom type for handling ObjectId
PyObjectId = str


# Function to create a new ObjectId string
def generate_objectid() -> PyObjectId:
    """Generate a new ObjectId as string."""
    return str(ObjectId())


# Validator function to ensure ObjectId is converted to string
def validate_object_id(v: Any) -> str:
    """Convert any ObjectId to string."""
    if isinstance(v, ObjectId):
        return str(v)
    return v


class BaseKubeWiseModel(BaseModel):
    """Base model with common configuration using Pydantic v2 style."""

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
        validate_assignment=True,
        frozen=False,
    )

    id: Optional[PyObjectId] = Field(
        default_factory=generate_objectid,
        alias="_id",
        description="Database document ID",
        json_schema_extra={"example": "60b0f0b0a0b0c0d0e0f01020", "type": "string"},
    )
    
    @field_validator("id", mode="before")
    def validate_id(cls, v):
        """Ensure id is always a string."""
        return serialize_object_id(v)
