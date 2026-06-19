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
import os

import httpx
from fastapi import FastAPI
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
    """Send an MCP tools/list request and return the tool names."""
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            # Step 1: initialize
            init_resp = await client.post(
                url,
                headers={**headers, "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26",
                                 "clientInfo": {"name": "status-probe", "version": "0"},
                                 "capabilities": {}}},
            )
            if init_resp.status_code != 200:
                return []
            # Step 2: tools/list
            tools_resp = await client.post(
                url,
                headers={**headers, "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            if tools_resp.status_code != 200:
                return []
            # Parse SSE or plain JSON
            text = tools_resp.text
            import json as _json
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                try:
                    obj = _json.loads(line)
                    tools = obj.get("result", {}).get("tools", [])
                    return [t["name"] for t in tools if isinstance(t, dict) and "name" in t]
                except Exception:
                    continue
    except Exception:
        pass
    return []


@app.get("/mcp-servers")
async def mcp_servers() -> list[dict]:
    """Return live status of each MCP server the agent is configured to connect to.

    Probes each server's tools/list endpoint so the dashboard reflects real
    connectivity, not hardcoded assumptions.
    """
    from agent import BRIDGE_URL, WEATHER_URL, REGISTRY_PIP_URL, _REGISTRY_PIP_API_KEY

    pip_key = _REGISTRY_PIP_API_KEY
    gateway_token_placeholder = "probe-no-user-token"  # tools/list doesn't need real auth on most servers

    servers_config = [
        {
            "name": "SpiceDB MCP Bridge",
            "url": BRIDGE_URL,
            "auth": "per-turn token exchange (PingOne)",
            "headers": {"Authorization": f"Bearer {gateway_token_placeholder}"},
        },
        {
            "name": "Weather",
            "url": WEATHER_URL,
            "auth": "per-turn token exchange (PingOne)",
            "headers": {"Authorization": f"Bearer {gateway_token_placeholder}"},
        },
        {
            "name": "Registry PIP",
            "url": REGISTRY_PIP_URL,
            "auth": "static bearer token",
            "headers": {"Authorization": f"Bearer {pip_key}"} if pip_key else {},
        },
    ]

    results = await asyncio.gather(*[
        _probe_mcp_tools(s["url"], s["headers"]) for s in servers_config
    ])

    return [
        {
            "name": s["name"],
            "url": s["url"],
            "auth": s["auth"],
            "tools": tools,
            "reachable": len(tools) > 0,
        }
        for s, tools in zip(servers_config, results)
    ]
