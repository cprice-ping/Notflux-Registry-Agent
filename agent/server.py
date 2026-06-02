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
