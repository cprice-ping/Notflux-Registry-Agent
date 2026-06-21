"""
mcp/models.py

Pydantic input models for the SpiceDB MCP Bridge tools.

Kept separate from server.py so they can be unit-tested without standing up the
FastMCP server. `VALID_TOKENS` is populated at startup from the live SpiceDB
schema (see server.py) and consulted by the validators to reject relation /
permission names that do not exist in the schema.

NOTE: deliberately no `from __future__ import annotations` here — Pydantic needs
the Literal types as real objects (not stringized forward refs) to build the
models without an explicit model_rebuild().
"""
from typing import Literal

from pydantic import BaseModel, field_validator

ObjectType = Literal["agent", "user", "mcp_server", "mcp_tool"]
SubjectType = Literal["agent", "user"]

# Populated at startup from the live SpiceDB schema; used by the validators.
VALID_TOKENS: list[str] = []


def set_valid_tokens(tokens: list[str]) -> None:
    """Replace the set of schema token names the validators accept."""
    global VALID_TOKENS
    VALID_TOKENS = tokens


class PermissionCheckArgs(BaseModel):
    resource_type: ObjectType
    resource_id: str
    permission: str
    subject_type: SubjectType
    subject_id: str

    @field_validator("permission")
    @classmethod
    def permission_must_be_valid(cls, v: str) -> str:
        if VALID_TOKENS and v not in VALID_TOKENS:
            raise ValueError(
                f"Unknown permission '{v}'. Valid names from schema: {VALID_TOKENS}"
            )
        return v


class RelationshipUpdateItem(BaseModel):
    operation: Literal["OPERATION_TOUCH", "OPERATION_DELETE"] = "OPERATION_TOUCH"
    resource_type: ObjectType
    resource_id: str
    relation: str
    subject_type: SubjectType
    subject_id: str
    subject_relation: str = ""

    @field_validator("relation")
    @classmethod
    def relation_must_be_valid(cls, v: str) -> str:
        if VALID_TOKENS and v not in VALID_TOKENS:
            raise ValueError(
                f"Unknown relation '{v}'. Valid names from schema: {VALID_TOKENS}"
            )
        return v
