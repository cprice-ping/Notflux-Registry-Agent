"""
agent/main.py

Registry Governor – Google ADK agent deployed to Vertex AI Agent Engine.

TOKEN FLOW:
  Frontend (Next.js)                    Vertex Agent Engine
  ──────────────────                    ──────────────────────────────────────
  OIDC login (PingOne)                  Session state:
    id_token ──[Exchange 1]──────────►  pingone_authorization: "Bearer agent_token"
    → agent_token (aud=registry-agent)            │
                                                  │  inject_mcp_auth reads state,
                                                  │  performs Exchange 2 per turn:
                                                  │  agent_token ──[Exchange 2]──► mcp_token
                                                  │  (aud=registry-mcp)                │
                                                  ▼                                    │
  POST /api/sessions                    McpToolset(                                    │
    pingone_authorization ──────────►     headers={"Authorization": mcp_token})        │
                                                  │                                    │
  POST /api/chat ──────────────────────────────►  │                                    │
                                                  ▼                                    ▼
                                        SpiceDB MCP Bridge validates aud=registry-mcp
                                        Forwards X-Remote-User / X-Remote-Agent headers
                                        into SpiceDB permission mutations

DEPLOYMENT:
  See deploy.sh — uses `adk deploy agent` targeting Vertex AI Agent Engine.

ENVIRONMENT VARIABLES:
  Required for PingOne token exchange (Exchange 2):
    PINGONE_ENV_ID              PingOne environment UUID
    PINGONE_CLIENT_ID           OAuth client used for the exchange
    PINGONE_CLIENT_SECRET       Client secret
    PINGONE_AGENT_AUDIENCE      Expected aud in the agent_token (aud check)
    PINGONE_MCP_SCOPE           Scope to request → resolves to MCP resource server aud
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Optional
from urllib.parse import quote

import requests as http_requests
from google.adk.agents import llm_agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BRIDGE_URL = os.getenv('MCP_BRIDGE_URL', 'https://notflux-gateway.ping-devops.com/mcp/agent-registry')
WEATHER_URL = os.getenv('WEATHER_MCP_URL', 'https://notflux-gateway.ping-devops.com/mcp/weather')

# Registry-PIP MCP endpoint — static bearer token (no per-turn exchange).
REGISTRY_PIP_URL     = os.getenv('REGISTRY_PIP_URL', 'https://notflux-registry-pip.ping-devops.com/mcp')
_REGISTRY_PIP_API_KEY = os.getenv('REGISTRY_PIP_API_KEY', '')

# PingOne Exchange 2 — agent_token → mcp_token
_PINGONE_ENV_ID         = os.getenv('PINGONE_ENV_ID', '')
_PINGONE_CLIENT_ID      = os.getenv('PINGONE_CLIENT_ID', '')
_PINGONE_CLIENT_SECRET  = os.getenv('PINGONE_CLIENT_SECRET', '')
_PINGONE_AGENT_AUDIENCE = os.getenv('PINGONE_AGENT_AUDIENCE', '')
_PINGONE_MCP_SCOPE      = os.getenv('PINGONE_MCP_SCOPE', '')

# Simple in-process cache: raw_agent_token → (mcp_token, expires_at)
_mcp_token_cache: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_vertex_agent_id() -> str:
    """Return the Vertex Agent Engine resource path to embed as agent_id claim."""
    project   = os.getenv('GCP_PROJECT') or os.getenv('GOOGLE_CLOUD_PROJECT') or os.getenv('CLOUD_ML_PROJECT_ID', '')
    location  = os.getenv('GCP_LOCATION') or os.getenv('GOOGLE_CLOUD_LOCATION') or os.getenv('CLOUD_ML_REGION', 'us-west1')
    engine_id = os.getenv('VERTEX_REASONING_ENGINE_ID', '')
    if project and engine_id:
        return f'projects/{project}/locations/{location}/reasoningEngines/{engine_id}'
    if project:
        return f'projects/{project}/locations/{location}/reasoningEngines/registry-governor'
    return ''


def _exchange_for_mcp_token(agent_token: str) -> str:
    """RFC 8693 token exchange: agent_token → mcp_token (aud = MCP bridge).

    Falls back to INTERNAL_M2M_KEY when PingOne env vars are not configured.
    """
    if not all([_PINGONE_ENV_ID, _PINGONE_CLIENT_ID, _PINGONE_CLIENT_SECRET, _PINGONE_MCP_SCOPE]):
        logging.warning('exchange_for_mcp: PingOne env vars not configured — using agent_token directly')
        return agent_token

    # Validate aud claim before the round-trip to PingOne.
    if _PINGONE_AGENT_AUDIENCE:
        try:
            parts  = agent_token.split(".")
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode())
            aud = payload.get("aud", [])
            if isinstance(aud, str):
                aud = [aud]
            if _PINGONE_AGENT_AUDIENCE not in aud:
                raise ValueError(f"aud={aud!r} does not contain {_PINGONE_AGENT_AUDIENCE!r}")
        except Exception as exc:
            raise RuntimeError(f"exchange_for_mcp: agent_token aud validation failed — {exc}")

    cached = _mcp_token_cache.get(agent_token)
    if cached and time.time() < cached[1]:
        logging.debug("exchange_for_mcp: cache hit")
        return cached[0]

    agent_id  = _get_vertex_agent_id()
    token_url = f"https://auth.pingone.com/{_PINGONE_ENV_ID}/as/token"
    basic_cred = base64.b64encode(
        f"{quote(_PINGONE_CLIENT_ID)}:{quote(_PINGONE_CLIENT_SECRET)}".encode()
    ).decode()

    body: dict[str, str] = {
        "grant_type":           "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token":        agent_token,
        "subject_token_type":   "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "scope":                _PINGONE_MCP_SCOPE,
    }
    if agent_id:
        body["agent_id"] = agent_id

    logging.info(f"exchange_for_mcp: POST {token_url} client_id={_PINGONE_CLIENT_ID} scope={_PINGONE_MCP_SCOPE} agent_id={agent_id or '(none)'}")
    resp = http_requests.post(
        token_url,
        data=body,
        headers={"Authorization": f"Basic {basic_cred}"},
        timeout=10,
    )
    resp.raise_for_status()

    result     = resp.json()
    mcp_token  = result["access_token"]
    expires_in = int(result.get("expires_in", 3600))
    _mcp_token_cache[agent_token] = (mcp_token, time.time() + expires_in - 30)
    # Log decoded claims for diagnostics (no raw token in logs)
    try:
        parts   = mcp_token.split(".")
        padded  = parts[1] + "=" * (-len(parts[1]) % 4)
        claims  = json.loads(base64.urlsafe_b64decode(padded).decode())
        exp_str = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(claims.get('exp', 0)))
        logging.warning(
            f"exchange_for_mcp: ok  sub={claims.get('sub')}  "
            f"aud={claims.get('aud')}  exp={exp_str}"
        )
    except Exception:
        logging.warning("exchange_for_mcp: ok (could not decode claims)")
    return mcp_token


# ---------------------------------------------------------------------------
# Before-agent callback — injects per-session MCP auth before each turn
# ---------------------------------------------------------------------------

def inject_mcp_auth(callback_context: CallbackContext) -> Optional[types.Content]:
    """Rebuild the McpToolset with the session's exchanged token before each turn.

    ag_ui_adk's extract_headers injects the frontend's x-agent-authorization
    header into ADK session state as state["headers"]["x-agent-authorization"].
    This callback reads it, runs Exchange 2 (agent_token → mcp_token), then
    attaches a fresh McpToolset so every turn is authenticated.
    """
    headers = callback_context.state.get("headers", {})
    # ag_ui_adk strips the x- prefix and converts hyphens to underscores:
    # x-agent-authorization → agent_authorization
    auth = headers.get("agent_authorization", "")
    logging.warning(f"inject_mcp_auth: auth_present={bool(auth)} headers_keys={list(headers.keys())}")
    if not auth:
        logging.warning("inject_mcp_auth: no x-agent-authorization header — MCP tools unavailable")
        return None

    agent_token = auth.removeprefix("Bearer ").strip()

    try:
        mcp_token = _exchange_for_mcp_token(agent_token)
    except Exception as exc:
        logging.error(f"inject_mcp_auth: token exchange failed — {exc}. MCP tools unavailable this turn.")
        return None

    mcp_auth = f"Bearer {mcp_token}"

    # ── Probe the actual MCP endpoint with auth (not /healthz which bypasses auth)
    try:
        # MCP initialize handshake — POST with the required Content-Type
        probe_resp = http_requests.post(
            BRIDGE_URL,
            headers={
                "Authorization": mcp_auth,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26",
                             "clientInfo": {"name": "probe", "version": "0"},
                             "capabilities": {}}},
            timeout=8,
            stream=False,
        )
        logging.warning(
            f"inject_mcp_auth: mcp_probe  url={BRIDGE_URL}  "
            f"status={probe_resp.status_code}  "
            f"content-type={probe_resp.headers.get('content-type', '?')!r}  "
            f"body={probe_resp.text[:200]!r}"
        )
    except Exception as probe_exc:
        logging.warning(f"inject_mcp_auth: mcp_probe failed — {probe_exc}")

    # Log the exact URL + auth header prefix being set on McpToolset
    token_preview = mcp_token[-8:] if len(mcp_token) > 8 else "<short>"
    logging.warning(
        f"inject_mcp_auth: McpToolset url={BRIDGE_URL}  "
        f"Authorization=Bearer ...[...{token_preview}]"
    )

    agent   = callback_context._invocation_context.agent
    non_mcp = [t for t in agent.tools if not isinstance(t, McpToolset)]

    new_toolsets: list[McpToolset] = [
        # SpiceDB bridge — per-turn PingOne exchanged token.
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=BRIDGE_URL,
                headers={"Authorization": mcp_auth},
            )
        ),
        # Weather MCP server — same PingGateway, same per-turn token.
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=WEATHER_URL,
                headers={"Authorization": mcp_auth},
            )
        ),
    ]

    # Registry-PIP — static bearer token (no per-turn exchange needed).
    if _REGISTRY_PIP_API_KEY:
        new_toolsets.append(
            McpToolset(
                connection_params=StreamableHTTPConnectionParams(
                    url=REGISTRY_PIP_URL,
                    headers={"Authorization": f"Bearer {_REGISTRY_PIP_API_KEY}"},
                )
            )
        )
    else:
        logging.warning("inject_mcp_auth: REGISTRY_PIP_API_KEY not set — Registry-PIP tools unavailable")

    agent.tools = non_mcp + new_toolsets
    return None


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------
root_agent = llm_agent.LlmAgent(
    name="registry_governor",
    model="gemini-2.5-flash",
    description=(
        "IAM Governance Agent for the Notflux Agent Registry. "
        "Manages SpiceDB permissions via natural language on behalf of "
        "human administrators."
    ),
    instruction="""
You are the dedicated IAM Governance Agent for the Notflux cluster.
You have exclusive access to the SpiceDB relationship graph through your tools.

────────────────────────────────────────────────────────────────────────────
IDENTITY RESOLUTION
────────────────────────────────────────────────────────────────────────────
• When a user says "me", "this agent", or "myself":
    - For agent subjects  → use subject_id="me"  (resolved from X-Remote-Agent)
    - For user subjects   → use subject_id="me"  (resolved from X-Remote-User)

• When provisioning access for the main operational Notflux agent, use its
  static Vertex AI resource ID as the subject_id:
    projects/3682147732/locations/us-central1/reasoningEngines/notflux-agent

────────────────────────────────────────────────────────────────────────────
SAFETY RULES  (never bypass these)
────────────────────────────────────────────────────────────────────────────
1. Only call update_relationships or write_schema if the request originates
   from a privileged human administrator (X-Remote-User is present and
   non-empty). Reject mutations from unauthenticated callers.

2. Before any destructive operation (OPERATION_DELETE, schema overwrite),
   call read_relationships or read_schema first and summarise what will
   change. Ask for explicit confirmation unless the user already confirmed.

3. Never expose raw preshared keys, token values, or internal service URLs
   in your replies.

────────────────────────────────────────────────────────────────────────────
WORKFLOW
────────────────────────────────────────────────────────────────────────────
• Use read_schema to understand the current permission model before advising.
• For any request to show, list, count, or summarise agents, MCP servers,
  tools, grants, or relationships, you MUST call read_relationships to fetch
  live data. Do not answer those requests from schema alone.
• If a list/count query would require multiple read_relationships calls,
  perform them all before answering, then merge the results in your response.
• Use check_permission to answer "can X do Y?" questions directly.
  VALID PERMISSIONS AND RELATIONS by object type (read from the schema):
    mcp_server  → relations:  authorized_agent, authorized_user, public_to_all_users
                   permissions: view_server
    mcp_tool    → relations:  parent_server, direct_agent
                   permissions: execute
    agent       → relations:  owner
  To check if an agent can use an mcp_server, use permission="authorized_agent"
  (it is treated as a relation check). Never invent permission names like
  "access", "use", or "call" — they do not exist and will always return DENIED.
• Use read_relationships to list existing grants before provisioning new ones.
• Summarise every relationship change in plain English after committing it.

────────────────────────────────────────────────────────────────────────────
ENTITY REGISTRY  (Registry-PIP tools: register_entity, resolve_entity)
────────────────────────────────────────────────────────────────────────────
The Registry-PIP is the name-to-ID source of truth for all entities in the
Notflux cluster. Every agent, user, or MCP server that needs a human-readable
name recorded, or whose ID you want to look up later, must be registered here.

• register_entity(id, type, name, owner_guid, metadata) — upsert an entity.
  - id:         Stable unique identifier (Vertex resource path, PingOne GUID,
                or any opaque string). This SAME id must be used in SpiceDB
                relationships.
  - type:       One of: "agent", "user", "mcp_server", "mcp_tool"
  - name:       Human-readable display name.
  - owner_guid: PingOne user GUID of the human who owns this entity.
  - metadata:   Optional JSON dict for extra context.

• resolve_entity(id) — look up a previously registered entity by id.
  Use this before calling update_relationships when you need to confirm an
  entity exists, or to display its human-readable name.

• list_entities(type=None) — list all registered entities, optionally
  filtered by type. Use this when an admin asks "what agents are registered?"
  or "show me everything in the registry". No ID required.

• find_entity_by_name(name, type=None) — case-insensitive substring search
  on the entity name. Use this when an admin asks about an entity by its
  human-readable name, e.g. "Tell me about the NotFlux Agent". Returns all
  matching records so you can then resolve_entity or check permissions by ID.
  ALWAYS use find_entity_by_name before resolve_entity when the admin has
  given you a name rather than an ID.

ONBOARDING WORKFLOW — follow this order every time a new agent or MCP server
is being added to the cluster:

  STEP 1 — Register in Registry-PIP:
    register_entity(
      id="<stable-resource-id>",
      type="agent",          # or mcp_server / user / mcp_tool
      name="<display-name>",
      owner_guid="<owner-pingone-guid>",
    )

  STEP 2 — Grant permissions in SpiceDB (use the SAME id):
    update_relationships(relationships=[
      { "resource_type": "agent",
        "resource_id":   "<stable-resource-id>",   # ← must match Step 1
        "relation":      "owner",
        "subject_type":  "user",
        "subject_id":    "<owner-subject-id>" }
    ], operation="OPERATION_TOUCH")

Never skip Step 1. An entity that exists in SpiceDB but not in Registry-PIP
cannot be resolved by name or surfaced to P1AZ for policy decisions.
""",
    before_agent_callback=inject_mcp_auth,
    tools=[
        # No McpToolset here — inject_mcp_auth adds one with the
        # session-specific mcp_token before every turn.
    ],
)

