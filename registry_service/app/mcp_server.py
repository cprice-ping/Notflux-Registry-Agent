"""
registry_service/app/mcp_server.py

FastMCP tool definitions for the Registry PIP.
These tools are consumed by the Conductor Agent to register and resolve entities.

Auth is handled by the _McpBearerAuth middleware in main.py — tools here
do not need to perform their own auth checks.
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .database import AsyncSessionLocal
from .models import Entity

mcp = FastMCP(
    name="registry-pip",
    instructions=(
        "Registry Policy Information Point. "
        "Use register_entity to create or update entity records (agents, MCP servers, users, etc.). "
        "Use resolve_entity to look up a canonical entity record by its stable ID. "
        "Entity IDs are the same identifiers used in SpiceDB relationship tuples."
    ),
)


@mcp.tool()
async def register_entity(
    id: str,
    type: str,
    name: str,
    owner_guid: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """
    Create or update an entity record in the Registry.

    This is an upsert — if an entity with the given id already exists it will
    be overwritten with the supplied values.

    Args:
        id:         Canonical identifier. Must match the object ID used in
                    SpiceDB relationship tuples (e.g. a GUID or a well-known slug).
        type:       Entity type string (e.g. "agent", "mcp_server", "user").
        name:       Human-friendly display name shown in the dashboard.
        owner_guid: GUID of the owning principal.
        metadata:   Optional key/value bag for additional attributes (stored as JSONB).

    Returns a confirmation string with the entity ID and whether it was
    newly created or updated.
    """
    async with AsyncSessionLocal() as session:
        existing = await session.get(Entity, id)
        if existing:
            existing.type = type
            existing.name = name
            existing.owner_guid = owner_guid
            existing.entity_metadata = metadata or {}
            action = "updated"
        else:
            session.add(
                Entity(
                    id=id,
                    type=type,
                    name=name,
                    owner_guid=owner_guid,
                    entity_metadata=metadata or {},
                )
            )
            action = "registered"
        await session.commit()

    return f"Entity '{id}' {action} successfully."


@mcp.tool()
async def resolve_entity(id: str) -> dict[str, Any]:
    """
    Resolve an entity by its canonical ID.

    Returns a dict with id, type, name, owner_guid, and metadata fields.
    Raises a ValueError (surfaced as an MCP tool error) if no entity with
    the given ID exists.

    Args:
        id: The canonical entity identifier to look up.
    """
    async with AsyncSessionLocal() as session:
        entity = await session.get(Entity, id)
        if entity is None:
            raise ValueError(f"Entity '{id}' not found in the Registry.")
        return {
            "id": entity.id,
            "type": entity.type,
            "name": entity.name,
            "owner_guid": entity.owner_guid,
            "metadata": entity.entity_metadata or {},
        }
