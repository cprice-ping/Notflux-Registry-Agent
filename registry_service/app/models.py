"""
registry_service/app/models.py

SQLAlchemy ORM model for the Entity table.

The Entity is the canonical source-of-truth record that maps a human-friendly
name to an internal GUID / infrastructure ID. P1AZ uses these records to
resolve friendly names before evaluating ReBAC policies in SpiceDB.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Entity(Base):
    """
    Canonical entity record.

    id          — primary key; the stable identifier used in SpiceDB / P1AZ
                  (e.g. a GUID or a well-known slug).
    type        — entity type string (e.g. "agent", "mcp_server", "user").
    name        — human-friendly display name.
    owner_guid  — GUID of the owning principal.
    entity_metadata — arbitrary key/value bag stored as JSONB (Python attribute
                  named entity_metadata to avoid collision with
                  DeclarativeBase.metadata; maps to the 'metadata' DB column).
    """

    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    owner_guid: Mapped[str] = mapped_column(String, nullable=False)
    entity_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=dict
    )
    # Workload identity fields — populated when the entity represents a
    # k8s Service Account or any principal whose OIDC sub contains characters
    # illegal in SpiceDB object IDs (colons, slashes, etc.).
    #
    # raw_sub  — the original OIDC sub claim (e.g.
    #            "system:serviceaccount:ping-devops-cprice:notflux-registry-agent")
    # sub_hash — SHA-256 hex of raw_sub, used as the SpiceDB object ID in
    #            relationship tuples. Always 64 hex chars; unique per sub.
    raw_sub: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    sub_hash: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    __table_args__ = (
        Index("ix_entities_type", "type"),
        Index("ix_entities_sub_hash", "sub_hash", unique=True),
    )
