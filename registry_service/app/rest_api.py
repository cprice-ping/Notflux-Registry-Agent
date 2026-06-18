"""
registry_service/app/rest_api.py

FastAPI router for the P1AZ REST interface.

This interface is designed for high-frequency, low-latency entity resolution
during P1AZ authorization decisions. Each request hits a single indexed PK
lookup against PostgreSQL.

Authentication:
  Kong handles full OIDC/JWT validation before requests reach this service.
  The service enforces that a non-empty Authorization header is present as a
  defence-in-depth guard against requests that bypass Kong entirely.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .models import Entity
from .schemas import EntityResponse


def _require_auth(authorization: Annotated[str | None, Header()] = None) -> str:
    """
    Require a non-empty Authorization header.

    Kong validates the JWT before the request reaches this service.
    This guard only ensures requests have not bypassed Kong entirely.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required.")
    return authorization


router = APIRouter(prefix="/v1", tags=["entities"])


@router.get(
    "/entities/{id}",
    response_model=EntityResponse,
    summary="Resolve entity by ID",
    description=(
        "Returns the canonical record for an entity. "
        "Designed for high-frequency consumption by P1AZ during authorization "
        "decisions — resolves a friendly name or stable ID to the full entity "
        "record including owner_guid and metadata."
    ),
)
async def get_entity(
    id: str,
    _auth: Annotated[str, Depends(_require_auth)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityResponse:
    entity: Entity | None = await session.get(Entity, id)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"Entity '{id}' not found.")
    return EntityResponse(
        id=entity.id,
        type=entity.type,
        name=entity.name,
        owner_guid=entity.owner_guid,
        metadata=entity.entity_metadata,
    )


@router.get(
    "/entities",
    response_model=list[EntityResponse],
    summary="List or search entities",
    description=(
        "Returns a list of entity records. Supports optional filtering by "
        "`type` and/or a case-insensitive name substring search via `name`. "
        "Results are sorted by name."
    ),
)
async def list_entities(
    _auth: Annotated[str, Depends(_require_auth)],
    session: Annotated[AsyncSession, Depends(get_session)],
    type: Annotated[str | None, Query(description="Filter by entity type (agent, user, mcp_server, mcp_tool).")] = None,
    name: Annotated[str | None, Query(description="Case-insensitive substring match on the entity name.")] = None,
) -> list[EntityResponse]:
    stmt = select(Entity).order_by(Entity.name)
    if type is not None:
        stmt = stmt.where(Entity.type == type)
    if name is not None:
        stmt = stmt.where(func.lower(Entity.name).contains(name.lower()))
    rows = (await session.execute(stmt)).scalars().all()
    return [
        EntityResponse(
            id=e.id,
            type=e.type,
            name=e.name,
            owner_guid=e.owner_guid,
            metadata=e.entity_metadata,
        )
        for e in rows
    ]
