"""
registry_service/app/schemas.py

Pydantic response schemas for the REST API.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class EntityResponse(BaseModel):
    id: str
    type: str
    name: str
    owner_guid: str
    metadata: dict[str, Any] | None = None

    model_config = {"from_attributes": True}
