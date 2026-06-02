"""
k8s/mcp/server.py

SpiceDB MCP Bridge – exposes SpiceDB permission management as MCP tools over
streamable HTTP transport so that remote agents (e.g. Google Vertex AI Agent
Builder) can provision and verify permissions via natural language.

Environment variables (all required unless noted):
  SPICEDB_ENDPOINT  – ClusterIP URL of the SpiceDB HTTP gateway,
                       e.g. http://spicedb.ping-devops-cprice.svc.cluster.local:8443
  SPICEDB_API_KEY   – SpiceDB preshared key (from spicedb-preshared-key secret)
  MCP_API_KEY       – Bearer token that remote agents must present to this server
                       (from spicedb-preshared-key/mcpApiKey secret key)
"""

from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from pydantic import BaseModel, field_validator
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SPICEDB_ENDPOINT: str = os.environ["SPICEDB_ENDPOINT"].rstrip("/")
SPICEDB_API_KEY: str  = os.environ["SPICEDB_API_KEY"]
MCP_API_KEY: str      = os.environ.get("MCP_API_KEY", "")

# Populated at startup from the live SpiceDB schema; used by Pydantic validators.
VALID_TOKENS: list[str] = []


def _spicedb_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SPICEDB_API_KEY}",
        "Content-Type":  "application/json",
    }


# ---------------------------------------------------------------------------
# Dynamic schema introspection
# ---------------------------------------------------------------------------

async def get_live_schema_tokens() -> list[str]:
    """Read the live SpiceDB schema and return all relation/permission names."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SPICEDB_ENDPOINT}/v1/schema/read",
                headers=_spicedb_headers(),
                json={},
                timeout=15,
            )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            schema_text = r.json().get("schemaText", "")
    except Exception:
        return []

    tokens = re.findall(r'\b(?:relation|permission)\s+(\w+)', schema_text)
    return list(dict.fromkeys(tokens))  # deduplicated, insertion-order preserved


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

class _BearerAuth(BaseHTTPMiddleware):
    """Reject requests that do not carry the expected MCP bearer token.
    /healthz is exempt so that Kubernetes probes always succeed.
    """

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if MCP_API_KEY:
            header = request.headers.get("Authorization", "")
            token  = header.removeprefix("Bearer ").strip()
            if token != MCP_API_KEY:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

ObjectType = Literal["agent", "user", "mcp_server", "mcp_tool"]
SubjectType = Literal["agent", "user"]


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


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="spicedb-mcp-bridge",
    instructions=(
        "Manage SpiceDB permissions for the Agent Registry. "
        "Use write_schema to update the permission model, "
        "update_relationships to provision agent → tool access, "
        "check_permission to verify whether an agent may execute a tool, "
        "and read_schema / read_relationships for inspection."
    ),
)


# ── Schema tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def read_schema() -> str:
    """Return the current SpiceDB permission schema text."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SPICEDB_ENDPOINT}/v1/schema/read",
            headers=_spicedb_headers(),
            json={},
            timeout=15,
        )
        # SpiceDB returns HTTP 404 / gRPC NOT_FOUND when no schema has been
        # written yet – that is not an application error, just an empty state.
        if r.status_code == 404:
            return "(no schema defined yet – use write_schema to create one)"
        r.raise_for_status()
        return r.json().get("schemaText", "")


@mcp.tool()
async def write_schema(schema: str) -> str:
    """
    Overwrite the SpiceDB permission schema.

    Args:
        schema: Full schema in Authzed schema language (.zed syntax).
                Must include all definition blocks – this is a full replacement,
                not a patch.

    Example:
        definition user {}
        definition agent {
            relation owner: user
        }
        definition mcp_tool {
            relation parent_server: mcp_server
            relation direct_agent: agent
            permission execute = direct_agent + parent_server->authorized_agent
        }
    """
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SPICEDB_ENDPOINT}/v1/schema/write",
            headers=_spicedb_headers(),
            json={"schema": schema},
            timeout=15,
        )
        r.raise_for_status()
        return "Schema written successfully."


# ── Relationship tools ───────────────────────────────────────────────────────

@mcp.tool()
async def update_relationships(updates: list[RelationshipUpdateItem]) -> str:
    """
    Write or delete one or more relationships in SpiceDB.

    Each item in `updates` is a RelationshipUpdateItem with fields:
        operation        – "OPERATION_TOUCH" (upsert, default) or "OPERATION_DELETE"
        resource_type    – Object type: "agent", "user", "mcp_server", or "mcp_tool"
        resource_id      – Object ID of the resource  (e.g. "search_web")
        relation         – Relation name from the schema (e.g. "direct_agent")
        subject_type     – "agent" or "user"
        subject_id       – Object ID of the subject, or "me" to resolve from headers
        subject_relation – (optional) sub-relation for group expansion

    subject_id="me" resolves automatically:
        subject_type="agent" → value of X-Remote-Agent header
        subject_type="user"  → value of X-Remote-User header

    Example – grant agent:agent-001 direct execute access to mcp_tool:search_web:
        [
          {
            "operation":     "OPERATION_TOUCH",
            "resource_type": "mcp_tool",
            "resource_id":   "search_web",
            "relation":      "direct_agent",
            "subject_type":  "agent",
            "subject_id":    "agent-001"
          }
        ]

    Example – grant the calling agent access to every tool on mcp_server:research:
        [
          {
            "operation":     "OPERATION_TOUCH",
            "resource_type": "mcp_server",
            "resource_id":   "research",
            "relation":      "authorized_agent",
            "subject_type":  "agent",
            "subject_id":    "me"
          }
        ]
    """
    _req = get_http_request()
    authenticated_user  = _req.headers.get("X-Remote-User") if _req else None
    authenticated_agent = _req.headers.get("X-Remote-Agent") if _req else None

    payload: list[dict] = []
    for u in updates:
        subject_id = u.subject_id
        if subject_id == "me":
            if u.subject_type == "agent":
                if not authenticated_agent:
                    return "Error: subject_id='me' for type 'agent' requires X-Remote-Agent header."
                subject_id = authenticated_agent
            elif u.subject_type == "user":
                if not authenticated_user:
                    return "Error: subject_id='me' for type 'user' requires X-Remote-User header."
                subject_id = authenticated_user
        subject: dict = {
            "object": {
                "objectType": u.subject_type,
                "objectId":   subject_id,
            }
        }
        if u.subject_relation:
            subject["optionalRelation"] = u.subject_relation

        payload.append({
            "operation": u.operation,
            "relationship": {
                "resource": {
                    "objectType": u.resource_type,
                    "objectId":   u.resource_id,
                },
                "relation": u.relation,
                "subject":  subject,
            },
        })

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SPICEDB_ENDPOINT}/v1/relationships/write",
            headers=_spicedb_headers(),
            json={"updates": payload},
            timeout=15,
        )
        r.raise_for_status()
        return f"{len(payload)} relationship(s) updated."


# ── Permission check ─────────────────────────────────────────────────────────

@mcp.tool()
async def check_permission(args: PermissionCheckArgs) -> str:
    """
    Check whether a subject holds a permission on a resource.

    Returns one of:
        PERMISSIONSHIP_HAS_PERMISSION
        PERMISSIONSHIP_NO_PERMISSION
        PERMISSIONSHIP_CONDITIONAL_PERMISSION

    Use subject_id="me" to check the calling agent or user automatically:
        subject_type="agent", subject_id="me"  → resolved from X-Remote-Agent header
        subject_type="user",  subject_id="me"  → resolved from X-Remote-User header

    Example – verify agent:agent-001 can execute mcp_tool:search_web:
        resource_type = "mcp_tool"
        resource_id   = "search_web"
        permission    = "execute"
        subject_type  = "agent"
        subject_id    = "agent-001"
    """
    subject_id = args.subject_id
    if subject_id == "me":
        _req = get_http_request()
        if args.subject_type == "agent":
            subject_id = (_req.headers.get("X-Remote-Agent", "") if _req else "")
            if not subject_id:
                return "Error: subject_id='me' for type 'agent' requires X-Remote-Agent header."
        elif args.subject_type == "user":
            subject_id = (_req.headers.get("X-Remote-User", "") if _req else "")
            if not subject_id:
                return "Error: subject_id='me' for type 'user' requires X-Remote-User header."
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SPICEDB_ENDPOINT}/v1/permissions/check",
            headers=_spicedb_headers(),
            json={
                "resource":   {"objectType": args.resource_type, "objectId": args.resource_id},
                "permission": args.permission,
                "subject":    {"object": {"objectType": args.subject_type, "objectId": subject_id}},
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("permissionship", "unknown")


# ── Relationship query ───────────────────────────────────────────────────────

@mcp.tool()
async def read_relationships(
    resource_type: ObjectType,
    resource_id: str = "",
    relation: str = "",
    subject_type: str = "",
    subject_id: str = "",
) -> list[str]:
    """
    Query existing relationships with optional filters.

    resource_type is required; all other parameters are optional filters.

    Returns a list of relationship strings in the format:
        resourceType:resourceId#relation@subjectType:subjectId
    """
    filter_obj: dict = {"resourceType": resource_type}
    if resource_id:
        filter_obj["optionalResourceId"] = resource_id
    if relation:
        filter_obj["optionalRelation"] = relation
    if subject_type:
        filter_obj["optionalSubjectFilter"] = {"subjectType": subject_type}
        if subject_id:
            filter_obj["optionalSubjectFilter"]["optionalSubjectId"] = subject_id

    results: list[str] = []
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SPICEDB_ENDPOINT}/v1/relationships/read",
            headers=_spicedb_headers(),
            json={"relationshipFilter": filter_obj},
            timeout=30,
        )
        r.raise_for_status()
        # SpiceDB streams newline-delimited JSON objects.
        for line in r.text.strip().splitlines():
            if not line:
                continue
            obj = json.loads(line)
            rel = obj.get("result", {}).get("relationship", {})
            res = rel.get("resource", {})
            sub = rel.get("subject", {}).get("object", {})
            results.append(
                f"{res.get('objectType')}:{res.get('objectId')}"
                f"#{rel.get('relation')}"
                f"@{sub.get('objectType')}:{sub.get('objectId')}"
            )
    return results


# ---------------------------------------------------------------------------
# ASGI application + entrypoint
# ---------------------------------------------------------------------------

def create_app() -> Starlette:
    """
    Wrap the FastMCP streamable-HTTP ASGI app with bearer-token auth middleware.
    The MCP endpoint is mounted at /mcp (matches the Ingress path).
    /healthz is an unauthenticated liveness/readiness target for K8s probes.
    """
    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    inner = mcp.http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(app):
        global VALID_TOKENS
        VALID_TOKENS = await get_live_schema_tokens()
        async with inner.lifespan(app):
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Mount("/", app=inner),
        ],
        lifespan=lifespan,
        middleware=[Middleware(_BearerAuth)],
    )


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="info")
