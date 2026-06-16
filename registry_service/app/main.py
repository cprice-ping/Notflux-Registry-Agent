"""
registry_service/app/main.py

Combined ASGI entrypoint for the Registry PIP service.

Routing:
  /mcp/*            FastMCP (Bearer token auth — MCP_API_KEY env var)
  /v1/entities/*    FastAPI REST (Kong-validated JWT, Authorization header required)
  /v1/docs          FastAPI Swagger UI
  /healthz          Unauthenticated liveness/readiness probe

Auth:
  MCP  — _McpBearerAuth middleware checks Authorization: Bearer <MCP_API_KEY>
          before passing the request to the FastMCP ASGI app.
  REST — Kong handles OIDC/JWT validation upstream; rest_api._require_auth()
          enforces that the Authorization header is present.

Database:
  On startup the lifespan handler runs SQLAlchemy create_all() to ensure the
  entities table and indexes exist. Safe to run on every boot (no-op if current).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from .database import engine
from .mcp_server import mcp
from .models import Base
from .rest_api import router

MCP_API_KEY: str = os.environ.get("MCP_API_KEY", "")


# ---------------------------------------------------------------------------
# MCP auth middleware — applies only to /mcp paths
# ---------------------------------------------------------------------------

class _McpBearerAuth(BaseHTTPMiddleware):
    """
    Enforce Bearer token auth on /mcp paths.
    /healthz and /v1/* are exempt — they handle auth independently.
    """

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)
        if MCP_API_KEY:
            header = request.headers.get("Authorization", "")
            token = header.removeprefix("Bearer ").strip()
            if token != MCP_API_KEY:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# FastAPI REST app
# ---------------------------------------------------------------------------

rest_app = FastAPI(
    title="Registry PIP",
    description=(
        "Policy Information Point — entity resolution for P1AZ and the Conductor Agent. "
        "Entities are the source-of-truth mapping between human-friendly names and "
        "the internal GUIDs/IDs used in SpiceDB ReBAC policies."
    ),
    version="1.0.0",
    docs_url="/v1/docs",
    openapi_url="/v1/openapi.json",
)
rest_app.include_router(router)


# ---------------------------------------------------------------------------
# Combined Starlette app + lifespan
# ---------------------------------------------------------------------------

def create_app() -> Starlette:
    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    mcp_asgi = mcp.http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(app):
        # Ensure DB schema is current on every boot (idempotent).
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with mcp_asgi.lifespan(app):
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Mount("/v1", app=rest_app),
            Mount("/", app=mcp_asgi),
        ],
        lifespan=lifespan,
        middleware=[Middleware(_McpBearerAuth)],
    )


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="info")
