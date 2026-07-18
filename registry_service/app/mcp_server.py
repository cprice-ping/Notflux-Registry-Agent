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
from .id_codec import to_external_id, to_storage_id
from .models import Entity

mcp = FastMCP(
    name="registry-pip",
    instructions=(
        "Registry Policy Information Point. "
        "Use register_entity to create or update entity records (agents, MCP servers, users, etc.). "
        "Use resolve_entity to look up a canonical entity record by its stable ID. "
        "Use list_entities to browse entities, optionally filtered by type. "
        "Use find_entity_by_name to search by human-readable name when the caller does not know the ID. "
        "Use delete_entity to remove stale records before re-registering updated data. "
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
    storage_id = to_storage_id(id)

    async with AsyncSessionLocal() as session:
        existing = await session.get(Entity, storage_id)
        if existing:
            existing.type = type
            existing.name = name
            existing.owner_guid = owner_guid
            existing.entity_metadata = metadata or {}
            action = "updated"
        else:
            session.add(
                Entity(
                    id=storage_id,
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
    storage_id = to_storage_id(id)

    async with AsyncSessionLocal() as session:
        # Try encoded lookup first, then raw for backward compatibility with
        # records created before ID encoding was introduced.
        entity = await session.get(Entity, storage_id)
        if entity is None and storage_id != id:
            entity = await session.get(Entity, id)
        if entity is None:
            raise ValueError(f"Entity '{id}' not found in the Registry.")
        return {
            "id": to_external_id(entity.id),
            "type": entity.type,
            "name": entity.name,
            "owner_guid": entity.owner_guid,
            "metadata": entity.entity_metadata or {},
        }


@mcp.tool()
async def list_entities(type: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """
    List entity records from the Registry.

    Args:
        type: Optional entity type filter (e.g. "agent", "mcp_server", "user").
        limit: Max rows to return (1-1000, default 200).
    """
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000

    async with AsyncSessionLocal() as session:
        stmt = select(Entity)
        if type:
            stmt = stmt.where(Entity.type == type)
        stmt = stmt.order_by(Entity.id.asc()).limit(limit)

        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": to_external_id(e.id),
                "type": e.type,
                "name": e.name,
                "owner_guid": e.owner_guid,
                "metadata": e.entity_metadata or {},
            }
            for e in rows
        ]


@mcp.tool()
async def find_entity_by_name(name: str, type: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """
    Search entities by fuzzy name match.

    Splits the query into words and checks that every word appears in either
    the entity's display name or its canonical ID (hostname slug). This means
    "Weather MCP server" will match an entity with id="weather-mcp-server"
    even if the words are separated by hyphens in the stored value.

    Args:
        name: Name fragment(s) to search for — space-separated words are each
              matched independently (AND logic) against both name and id fields.
        type: Optional entity type filter.
        limit: Max rows to return (1-1000, default 200).
    """
    from sqlalchemy import and_, or_

    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000

    words = [w for w in name.lower().split() if w]
    if not words:
        return []

    async with AsyncSessionLocal() as session:
        stmt = select(Entity)
        # Every word must appear in either the name or the id field.
        word_conditions = [
            or_(
                func.lower(Entity.name).contains(word),
                func.lower(Entity.id).contains(word),
            )
            for word in words
        ]
        stmt = stmt.where(and_(*word_conditions))
        if type:
            stmt = stmt.where(Entity.type == type)
        stmt = stmt.order_by(Entity.name.asc()).limit(limit)

        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": to_external_id(e.id),
                "type": e.type,
                "name": e.name,
                "owner_guid": e.owner_guid,
                "metadata": e.entity_metadata or {},
            }
            for e in rows
        ]


@mcp.tool()
async def delete_entity(id: str) -> str:
    """
    Delete an entity record by canonical ID.

    Supports both encoded-at-rest IDs and legacy raw IDs.
    """
    storage_id = to_storage_id(id)

    async with AsyncSessionLocal() as session:
        entity = await session.get(Entity, storage_id)
        if entity is None and storage_id != id:
            entity = await session.get(Entity, id)
        if entity is None:
            raise ValueError(f"Entity '{id}' not found in the Registry.")
        await session.delete(entity)
        await session.commit()
    return f"Entity '{id}' deleted successfully."
