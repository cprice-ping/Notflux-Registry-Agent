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

import requests as http_requests
from google.adk.agents import llm_agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BRIDGE_URL        = os.getenv('MCP_BRIDGE_URL', 'https://notflux-gateway.ping-devops.com/mcp/agent-registry')
WEATHER_URL       = os.getenv('WEATHER_MCP_URL', 'https://notflux-gateway.ping-devops.com/mcp/weather')

# Registry-PIP — static bearer token (no per-turn exchange needed)
REGISTRY_PIP_URL      = os.getenv('REGISTRY_PIP_URL', 'https://notflux-registry-pip.ping-devops.com/mcp')
_REGISTRY_PIP_API_KEY = os.getenv('REGISTRY_PIP_API_KEY', '')

# DaVinci-based token exchange — actor_token (k8s SA) + subject_token (human OIDC) → mcp_token
_DAVINCI_POLICY_URL     = os.getenv(
    'DAVINCI_POLICY_URL',
    'https://orchestrate-api.pingone.com/v1/company/59bb6a66-e76e-490c-b83a-884c50423da4'
    '/policy/cba6c296e71c5f569526fc99dc1a7be2/start',
)
_DAVINCI_POLICY_API_KEY = os.getenv('DAVINCI_POLICY_API_KEY', '')
_PINGONE_AGENT_AUDIENCE = os.getenv('PINGONE_AGENT_AUDIENCE', '')
_SA_TOKEN_PATH          = os.getenv('PINGONE_SA_TOKEN_PATH', '/var/run/secrets/tokens/davinci-token')

# Opt-in escape hatch for local development only.
_ALLOW_TOKEN_PASSTHROUGH = os.getenv('ALLOW_TOKEN_PASSTHROUGH', '').lower() in ('1', 'true', 'yes')

# Simple in-process cache: raw_agent_token → (mcp_token, expires_at)
_mcp_token_cache: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_sa_token() -> str:
    """Read the projected k8s Service Account token from disk.

    The kubelet rotates this automatically; always read fresh from disk
    rather than caching it — the file is in memory (tmpfs) so the read
    is cheap.
    """
    if not os.path.exists(_SA_TOKEN_PATH):
        raise RuntimeError(
            f"K8s Projected Token missing - check deployment manifest allocation "
            f"(expected at {_SA_TOKEN_PATH})"
        )
    with open(_SA_TOKEN_PATH, 'r') as f:
        return f.read().strip()


def _exchange_for_mcp_token(agent_token: str) -> str:
    """DaVinci-based token exchange: subject_token (human) + actor_token (k8s SA) → mcp_token.

    Calls the DaVinci policy flow instead of the PingOne /as/token endpoint so
    the actor_token (third-party k8s OIDC JWT) can be validated and embedded as
    the verified workload identity before PingOne mints the outbound access_token.
    """
    if not _DAVINCI_POLICY_API_KEY:
        if _ALLOW_TOKEN_PASSTHROUGH:
            logging.warning(
                'exchange_for_mcp: DAVINCI_POLICY_API_KEY not set and '
                'ALLOW_TOKEN_PASSTHROUGH enabled — forwarding agent_token directly '
                '(dev only; wrong audience for the gateway).'
            )
            return agent_token
        raise RuntimeError(
            'exchange_for_mcp: DAVINCI_POLICY_API_KEY not set — refusing to forward '
            'the agent_token (wrong audience for the gateway). Set the key, or set '
            'ALLOW_TOKEN_PASSTHROUGH=true for local development.'
        )

    # Sanity-check the aud claim on the incoming human token before the round-trip.
    # NOTE: decodes WITHOUT verifying signature — cheap pre-filter only.
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

    actor_token = _read_sa_token()

    logging.info(f"exchange_for_mcp: POST {_DAVINCI_POLICY_URL}")
    resp = http_requests.post(
        _DAVINCI_POLICY_URL,
        json={
            "actor_token":   actor_token,
            "subject_token": agent_token,
        },
        headers={
            "Content-Type": "application/json",
            "X-SK-API-Key": _DAVINCI_POLICY_API_KEY,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"exchange_for_mcp: DaVinci policy returned {resp.status_code} — "
            f"aborting gateway routing. body={resp.text[:200]!r}"
        )

    result = resp.json()
    if not result.get("success"):
        raise RuntimeError(
            f"exchange_for_mcp: DaVinci policy denied — "
            f"aborting gateway routing. body={str(result)[:200]!r}"
        )
    mcp_token  = result["access_token"]
    expires_in = int(result.get("expires_in", 3600))
    _mcp_token_cache[agent_token] = (mcp_token, time.time() + expires_in - 30)
    # Log decoded claims for diagnostics (no raw token in logs).
    try:
        parts   = mcp_token.split(".")
        padded  = parts[1] + "=" * (-len(parts[1]) % 4)
        claims  = json.loads(base64.urlsafe_b64decode(padded).decode())
        exp_str = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(claims.get('exp', 0)))
        logging.warning(
            f"exchange_for_mcp: ok  sub={claims.get('sub')}  "
            f"act={claims.get('act')}  aud={claims.get('aud')}  exp={exp_str}"
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

    # Decode and log the exchanged token claims (no sig verification — debug only).
    try:
        import json as _json
        _payload = mcp_token.split(".")[1]
        _payload += "=" * (-len(_payload) % 4)
        _claims = _json.loads(base64.b64decode(_payload).decode())
        logging.warning(
            f"inject_mcp_auth: mcp_token claims — "
            f"sub={_claims.get('sub')!r} "
            f"aud={_claims.get('aud')!r} "
            f"iss={_claims.get('iss')!r} "
            f"scope={_claims.get('scope')!r} "
            f"act={_claims.get('act')!r} "
            f"exp={_claims.get('exp')}"
        )
    except Exception as _dec_exc:
        logging.warning(f"inject_mcp_auth: mcp_token decode failed — {_dec_exc}")

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

    agent     = callback_context._invocation_context.agent
    non_mcp   = [t for t in agent.tools if not isinstance(t, McpToolset)]

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
• Registry-PIP ID handling:
    - DO NOT request, mention, or depend on any `sub_hash` field.
    - `register_entity` and `resolve_entity` use canonical IDs directly.
    - For DIDs (for example did:web:...), the registry performs reversible
        encoding internally and returns canonical DID values to callers.
    - If the caller asks to browse/search entities, use `list_entities` or
        `find_entity_by_name`; use `delete_entity` only when asked to remove stale
        records.
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

