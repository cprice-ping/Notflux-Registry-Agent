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
from sqlalchemy import func, select

from .database import AsyncSessionLocal
from .models import Entity

mcp = FastMCP(
    name="registry-pip",
    instructions=(
        "Registry Policy Information Point. "
        "Use register_entity to create or update entity records (agents, MCP servers, users, etc.). "
        "Use resolve_entity to look up a canonical entity record by its stable ID. "
        "Use list_entities to browse all registered entities (optionally filtered by type) when you do not know the ID. "
        "Use find_entity_by_name to search by human-readable name when an admin asks about an entity by name rather than ID. "
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


@mcp.tool()
async def list_entities(type: str | None = None) -> list[dict[str, Any]]:
    """
    List all registered entities, optionally filtered by type.

    Use this to browse what is in the Registry without needing to know IDs
    upfront. Returns a list of entity records sorted by name.

    Args:
        type: Optional filter — one of "agent", "user", "mcp_server",
              "mcp_tool". If omitted, all entities are returned.

    Returns a list of dicts, each with id, type, name, owner_guid, metadata.
    """
    async with AsyncSessionLocal() as session:
        stmt = select(Entity).order_by(Entity.name)
        if type is not None:
            stmt = stmt.where(Entity.type == type)
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": e.id,
            "type": e.type,
            "name": e.name,
            "owner_guid": e.owner_guid,
            "metadata": e.entity_metadata or {},
        }
        for e in rows
    ]


@mcp.tool()
async def find_entity_by_name(name: str, type: str | None = None) -> list[dict[str, Any]]:
    """
    Search for entities whose name contains the given string (case-insensitive).

    Use this when an admin asks about an entity by its human-readable name
    rather than by ID — e.g. "Tell me about the NotFlux Agent".

    Args:
        name: Substring to search for within the entity name field.
        type: Optional type filter — one of "agent", "user", "mcp_server",
              "mcp_tool". Narrows results to a single entity class.

    Returns a list of matching entity dicts (may be empty if nothing matches).
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Entity)
            .where(func.lower(Entity.name).contains(name.lower()))
            .order_by(Entity.name)
        )
        if type is not None:
            stmt = stmt.where(Entity.type == type)
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": e.id,
            "type": e.type,
            "name": e.name,
            "owner_guid": e.owner_guid,
            "metadata": e.entity_metadata or {},
        }
        for e in rows
    ]
