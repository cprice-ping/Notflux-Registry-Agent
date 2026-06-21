"""
Integration tests for the Registry PIP entity tools (registry_service/app).

Exercises the real register → resolve → delete path against a Postgres instance,
asserting that a workload-identity `sub` is SHA-256 hashed into a SpiceDB-safe
`sub_hash` (64 hex chars) and round-trips through resolve.

Requires DATABASE_URL (postgresql+asyncpg://...) — skipped otherwise, so the
suite stays runnable locally without a database.
"""
import hashlib
import os
import pathlib
import re
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


async def test_register_resolves_with_safe_sub_hash():
    from app.mcp_server import delete_entity, register_entity, resolve_entity

    sub = "system:serviceaccount:ping-devops-cprice:notflux-registry-agent"
    expected = hashlib.sha256(sub.encode()).hexdigest()

    try:
        result = await _fn(register_entity)(
            id="test-workload", type="agent", name="Workload Test",
            owner_guid="owner-guid", sub=sub,
        )
        assert expected in result
        assert re.fullmatch(r"[a-f0-9]{64}", expected)  # valid SpiceDB object id

        record = await _fn(resolve_entity)(id="test-workload")
        assert record["sub_hash"] == expected
        assert record["name"] == "Workload Test"
    finally:
        await _fn(delete_entity)(id="test-workload")


async def test_register_without_sub_has_no_hash():
    from app.mcp_server import delete_entity, register_entity, resolve_entity

    try:
        await _fn(register_entity)(
            id="test-plain", type="user", name="Plain User", owner_guid="owner-guid",
        )
        record = await _fn(resolve_entity)(id="test-plain")
        assert record["sub_hash"] is None
    finally:
        await _fn(delete_entity)(id="test-plain")
