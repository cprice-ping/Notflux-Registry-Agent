"""
Integration tests for Registry PIP entity tools (registry_service/app).

Exercises the real register → resolve path against Postgres and verifies
reversible DID handling:
    - register_entity encodes unsafe IDs for storage
    - resolve_entity returns the original caller-facing DID

Requires DATABASE_URL (postgresql+asyncpg://...) — skipped otherwise.
"""
import os
import pathlib
import sys

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — needs a Postgres instance",
)

# Make the `app` package importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "registry_service"))


def _fn(tool):
    """FastMCP wraps tools; the raw coroutine is exposed as `.fn`."""
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
async def _schema():
    from app.database import engine
    from app.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        # Dispose the global engine so no pooled asyncpg connection (bound to
        # this test's event loop) leaks into the next test's loop. pytest-asyncio
        # gives each test function a fresh loop; reusing a pooled connection
        # across loops raises "attached to a different loop".
        await engine.dispose()


async def test_register_resolves_with_encoded_id():
    from app.database import AsyncSessionLocal
    from app.id_codec import to_storage_id
    from app.mcp_server import register_entity, resolve_entity
    from app.models import Entity

    did = "did:web:reg.example.com:agents:napanode01"
    storage_id = to_storage_id(did)
    assert storage_id != did

    result = await _fn(register_entity)(
        id=did,
        type="agent",
        name="Napa Node",
        owner_guid="owner-guid",
    )
    assert "registered" in result or "updated" in result

    record = await _fn(resolve_entity)(id=did)
    assert record["id"] == did
    assert record["name"] == "Napa Node"

    # Confirm encoded-at-rest behavior.
    async with AsyncSessionLocal() as session:
        entity = await session.get(Entity, storage_id)
        assert entity is not None
        await session.delete(entity)
        await session.commit()


async def test_register_without_sub_has_no_hash():
    from app.database import AsyncSessionLocal
    from app.mcp_server import register_entity, resolve_entity
    from app.models import Entity

    await _fn(register_entity)(
        id="plain_user_001",
        type="user",
        name="Plain User",
        owner_guid="owner-guid",
    )

    record = await _fn(resolve_entity)(id="plain_user_001")
    assert record["id"] == "plain_user_001"

    # Cleanup
    async with AsyncSessionLocal() as session:
        entity = await session.get(Entity, "plain_user_001")
        if entity is not None:
            await session.delete(entity)
            await session.commit()
