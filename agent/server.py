"""
agent/server.py

AG-UI FastAPI server wrapping the Registry Governor ADK agent.

This is the Cloud Run entry point.  It exposes the ADK agent over the AG-UI
protocol so CopilotKit on the frontend can:
  - Stream thinking steps / tool calls live
  - Render structured relationship data as custom UI components
  - Pause execution for HITL approval before destructive SpiceDB mutations

AUTH FLOW (per-request headers — no Vertex session state needed):
  CopilotKit frontend sends:
    x-agent-authorization: Bearer <agent_token>   ← PingOne Exchange 1 result

  ag_ui_adk extract_headers puts it into ADK session state as:
    state["headers"]["x-agent-authorization"]

  inject_mcp_auth (before_agent_callback) reads this, runs Exchange 2:
    agent_token → mcp_token (aud = MCP bridge resource server)

  McpToolset is rebuilt per-turn with Authorization: Bearer <mcp_token>

DEPLOYMENT:
  See deploy.sh — builds a container image and deploys to Cloud Run.

RUNNING LOCALLY:
  uvicorn server:app --reload --port 8080
"""

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI, Header
from google.adk.apps import App, ResumabilityConfig

from ag_ui_adk import ADKAgent, add_adk_fastapi_endpoint

from agent import root_agent  # noqa: E402 — must come after ADK imports

# ---------------------------------------------------------------------------
# ADK App — ResumabilityConfig enables native HITL pause/resume
# ---------------------------------------------------------------------------
adk_app = App(
    name="registry_governor_app",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)

adk_agent_wrapper = ADKAgent.from_app(
    adk_app,
    session_timeout_seconds=3600,
    use_in_memory_services=True,
)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Registry Governor", version="1.0.0")

add_adk_fastapi_endpoint(
    app,
    adk_agent_wrapper,
    path="/",
    # Inject PingOne agent_token from the frontend into ADK session state so
    # inject_mcp_auth can perform Exchange 2 before each turn.
    extract_headers=["x-agent-authorization"],
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


async def _probe_mcp_tools(url: str, headers: dict) -> list[str]:
    """POST tools/list directly — FastMCP streamable-HTTP is stateless per request."""
    import json as _json
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                url,
                headers={**headers, "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            if resp.status_code != 200:
                logging.warning(f"_probe_mcp_tools: {url} status={resp.status_code}")
                return []
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                    tools = obj.get("result", {}).get("tools", [])
                    if tools is not None:
                        names = [t["name"] for t in tools if isinstance(t, dict) and "name" in t]
                        logging.info(f"_probe_mcp_tools: {url} found {names}")
                        return names
                except Exception:
                    continue
    except Exception as exc:
        logging.warning(f"_probe_mcp_tools: {url} exception={exc}")
    return []


async def _check_reachable(url: str) -> bool:
    """Any HTTP response (even 401/403) means the server is up."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                url,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
            )
            logging.info(f"_check_reachable: {url} status={resp.status_code}")
            return resp.status_code < 500
    except Exception as exc:
        logging.warning(f"_check_reachable: {url} exception={exc}")
        return False


@app.get("/mcp-servers")
async def mcp_servers(
    x_agent_authorization: str | None = Header(default=None),
) -> list[dict]:
    """Return live status of each MCP server the agent is configured to connect to.

    When the caller provides x-agent-authorization (the user's agent_token from
    PingOne Exchange 1), this endpoint performs Exchange 2 to obtain a real gateway
    token and probes each gateway-protected server with it.  The reachable/tools
    result therefore reflects actual policy — if the gateway denies the agent,
    the server shows as unreachable.

    Registry PIP uses its static bearer key and is always fully live-probed.
    """
    from agent import (
        BRIDGE_URL, WEATHER_URL, REGISTRY_PIP_URL, _REGISTRY_PIP_API_KEY,
        _exchange_for_mcp_token,
    )

    pip_headers = {"Authorization": f"Bearer {_REGISTRY_PIP_API_KEY}"} if _REGISTRY_PIP_API_KEY else {}

    # Attempt Exchange 2 if we have an agent token.
    gateway_headers: dict = {}
    if x_agent_authorization:
        agent_token = x_agent_authorization.removeprefix("Bearer ").strip()
        try:
            mcp_token = _exchange_for_mcp_token(agent_token)
            gateway_headers = {"Authorization": f"Bearer {mcp_token}"}
            logging.info("mcp-servers probe: exchange ok, probing with real gateway token")
        except Exception as exc:
            logging.warning(f"mcp-servers probe: exchange failed — {exc}; falling back to reachability-only")

    if gateway_headers:
        # Full tools/list probe with real token.
        bridge_tools, weather_tools, pip_tools = await asyncio.gather(
            _probe_mcp_tools(BRIDGE_URL, gateway_headers),
            _probe_mcp_tools(WEATHER_URL, gateway_headers),
            _probe_mcp_tools(REGISTRY_PIP_URL, pip_headers),
        )
        return [
            {
                "name": "SpiceDB MCP Bridge",
                "url": BRIDGE_URL,
                "auth": "per-turn token exchange (PingOne)",
                "tools": bridge_tools,
                "reachable": len(bridge_tools) > 0,
            },
            {
                "name": "Weather",
                "url": WEATHER_URL,
                "auth": "per-turn token exchange (PingOne)",
                "tools": weather_tools,
                "reachable": len(weather_tools) > 0,
            },
            {
                "name": "Registry PIP",
                "url": REGISTRY_PIP_URL,
                "auth": "static bearer token",
                "tools": pip_tools,
                "reachable": len(pip_tools) > 0,
            },
        ]
    else:
        # No agent token — fall back to unauthenticated reachability check + static tool lists.
        bridge_reachable, weather_reachable, pip_tools = await asyncio.gather(
            _check_reachable(BRIDGE_URL),
            _check_reachable(WEATHER_URL),
            _probe_mcp_tools(REGISTRY_PIP_URL, pip_headers),
        )
        return [
            {
                "name": "SpiceDB MCP Bridge",
                "url": BRIDGE_URL,
                "auth": "per-turn token exchange (PingOne)",
                "tools": ["read_schema", "write_schema", "read_relationships",
                          "update_relationships", "check_permission"],
                "reachable": bridge_reachable,
            },
            {
                "name": "Weather",
                "url": WEATHER_URL,
                "auth": "per-turn token exchange (PingOne)",
                "tools": [],
                "reachable": weather_reachable,
            },
            {
                "name": "Registry PIP",
                "url": REGISTRY_PIP_URL,
                "auth": "static bearer token",
                "tools": pip_tools,
                "reachable": len(pip_tools) > 0,
            },
        ]
