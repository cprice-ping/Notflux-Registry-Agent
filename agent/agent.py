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
    logging.info("exchange_for_mcp: ok")
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
    logging.info(f"inject_mcp_auth: auth_present={bool(auth)} headers_keys={list(headers.keys())}")
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

    agent     = callback_context._invocation_context.agent
    non_mcp   = [t for t in agent.tools if not isinstance(t, McpToolset)]
    agent.tools = non_mcp + [
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=BRIDGE_URL,
                headers={"Authorization": mcp_auth},
            )
        ),
    ]
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
""",
    before_agent_callback=inject_mcp_auth,
    tools=[
        # No McpToolset here — inject_mcp_auth adds one with the
        # session-specific mcp_token before every turn.
    ],
)

