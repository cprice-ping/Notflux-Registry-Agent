"""
Unit tests for the SpiceDB MCP Bridge Pydantic validators (mcp/models.py).

These guard the input contract the LLM must satisfy: object/subject types are
constrained to the schema's definitions, and relation/permission names are
checked against the live schema tokens so invented names are rejected before
any SpiceDB call is made. No network or SpiceDB instance is required.
"""
import importlib.util
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_models():
    spec = importlib.util.spec_from_file_location("bridge_models", _ROOT / "mcp" / "models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = _load_models()


def test_relationship_accepts_known_relation():
    bridge.VALID_TOKENS = ["owner", "direct_agent"]
    item = bridge.RelationshipUpdateItem(
        resource_type="agent", resource_id="x",
        relation="owner", subject_type="user", subject_id="y",
    )
    assert item.relation == "owner"
    assert item.operation == "OPERATION_TOUCH"  # default upsert


def test_relationship_rejects_unknown_relation():
    bridge.VALID_TOKENS = ["owner", "direct_agent"]
    with pytest.raises(Exception):
        bridge.RelationshipUpdateItem(
            resource_type="agent", resource_id="x",
            relation="totally_made_up", subject_type="user", subject_id="y",
        )


def test_relationship_rejects_unknown_object_type():
    bridge.VALID_TOKENS = ["owner"]
    with pytest.raises(Exception):
        bridge.RelationshipUpdateItem(
            resource_type="planet", resource_id="x",
            relation="owner", subject_type="user", subject_id="y",
        )


def test_permission_check_rejects_unknown_permission():
    bridge.VALID_TOKENS = ["execute", "view_server"]
    with pytest.raises(Exception):
        bridge.PermissionCheckArgs(
            resource_type="mcp_tool", resource_id="t",
            permission="call",  # not a real permission
            subject_type="agent", subject_id="a",
        )


def test_validation_is_permissive_when_schema_unreadable():
    # When startup could not read the schema, VALID_TOKENS is empty and the
    # validator falls back to permissive — it must not block all writes.
    bridge.VALID_TOKENS = []
    item = bridge.RelationshipUpdateItem(
        resource_type="agent", resource_id="x",
        relation="anything", subject_type="user", subject_id="y",
    )
    assert item.relation == "anything"
