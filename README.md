# Agent Registry

A fine-grained authorization registry for agentic AI systems, built on [SpiceDB](https://github.com/authzed/spicedb).

Agents and MCP servers are modeled as first-class resources. An ADK-based governor agent manages the registry through natural language, while a Next.js dashboard gives administrators live visibility and Human-in-the-Loop control over every permission change.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser                                                        │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  Next.js Dashboard  (CopilotKit v2)                       │  │
│  │  • Reactive relationship table & metrics cards            │  │
│  │  • Inline tool renderers (schema, relationships, checks)  │  │
│  │  • PingOne PKCE login + token exchange                    │  │
│  └────────────────────┬──────────────────────────────────────┘  │
└───────────────────────│─────────────────────────────────────────┘
                        │  AG-UI (CopilotKit protocol)
                        ▼
         ┌──────────────────────────────┐
         │  Registry Governor           │
         │  (Google ADK + ag_ui_adk)    │
         │  • LlmAgent (Gemini)         │
         │  • Per-turn MCP token exch.  │
         └──────────────┬───────────────┘
                        │  MCP streamable-HTTP
                        ▼
         ┌──────────────────────────────┐
         │  SpiceDB MCP Bridge          │
         │  (FastMCP + Starlette)       │
         │  • read/write_schema         │
         │  • update_relationships      │
         │  • check_permission          │
         │  • read_relationships        │
         │  • Live schema token check   │
         └──────────────┬───────────────┘
                        │  SpiceDB HTTP REST API
                        ▼
         ┌──────────────────────────────┐
         │  SpiceDB                     │
         │  Zanzibar-compatible         │
         │  permission graph            │
         └──────────────────────────────┘
```

All three application components run in the `ping-devops-cprice` Kubernetes namespace, co-located with SpiceDB.

---

## Repository Structure

```
.
├── schema/
│   └── schema.zed              # SpiceDB permission model
├── frontend/                   # Next.js dashboard (CopilotKit v2)
│   ├── src/app/
│   │   ├── page.tsx            # Main dashboard: auth, state, tool renderers
│   │   └── api/
│   │       ├── copilotkit/     # CopilotKit → AG-UI proxy endpoint
│   │       └── oidc/callback/  # PingOne OIDC code + token exchange
│   └── Dockerfile
├── agent/                      # Registry Governor (ADK agent)
│   ├── agent.py                # LlmAgent definition + PingOne token exchange
│   ├── server.py               # AG-UI FastAPI server entry point
│   ├── requirements.txt
│   └── Dockerfile
├── k8s/
│   ├── deployment.yaml         # SpiceDB deployment + ClusterIP service
│   ├── frontend.yaml           # Frontend deployment, service, ingress
│   ├── registry-agent.yaml     # Agent deployment, service, ingress
│   ├── mcp-bridge.yaml         # MCP Bridge deployment, service, ingress
│   ├── patch-p1az.yaml         # PingOne Advanced Services gateway patch
│   ├── mcp/                    # SpiceDB MCP Bridge source
│   │   ├── server.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   └── secrets.yaml            # ⚠ Not committed — see Configuration below
├── scripts/
│   └── bootstrap.sh            # One-shot cluster apply script
└── .gitignore
```

---

## Permission Model

The SpiceDB schema (`schema/schema.zed`) defines four object types:

| Type | Description |
|---|---|
| `user` | A human principal (administrator) |
| `agent` | An autonomous AI agent (e.g. a Vertex AI reasoning engine) |
| `mcp_server` | A named collection of MCP tools |
| `mcp_tool` | A single callable MCP tool |

### Relations & permissions

```
agent
  └── owner: user                      — which human owns this agent

mcp_server
  ├── authorized_agent: agent          — agent has blanket access to all tools on this server
  ├── authorized_user: user            — user can view this server
  └── public_to_all_users: user:*      — open to all authenticated users
      └── view_server (permission) = authorized_user + public_to_all_users

mcp_tool
  ├── parent_server: mcp_server        — which server this tool belongs to
  └── direct_agent: agent              — agent has direct access to this specific tool
      └── execute (permission) = direct_agent + parent_server->authorized_agent
```

An agent can execute a tool if it has **either** a `direct_agent` relationship on the tool **or** an `authorized_agent` relationship on the tool's parent server.

---

## Auth & Token Flow

The system uses PingOne as its identity provider with a two-hop token exchange:

```
1. Browser                  2. Frontend (server-side)       3. Agent (per-turn)
───────────────────         ──────────────────────────      ───────────────────
PKCE login (PingOne)   →    Exchange 1:                 →   Exchange 2:
  id_token / code           person_token                    agent_token
                            (aud = registry-person)    →    mcp_token
                                  │                         (aud = registry-mcp)
                            Exchange 1b:                         │
                            agent_token                          ▼
                            (aud = registry-agent)       McpToolset per turn
                            stored as httpOnly cookie     with Authorization: Bearer mcp_token
                            registry_agent_token
```

**Key detail:** The `registry_agent_token` cookie is set by `/api/oidc/callback` on every login _and_ on every page load for cached sessions (the frontend re-runs Exchange 1b on mount if a `registry_person_token` is already in sessionStorage). This ensures the agent always has a valid MCP token even after the container restarts.

---

## Components

### Frontend — `frontend/`

Next.js 15 app using **CopilotKit v2** (`@copilotkit/react-core/v2`).

- **Auth**: PingOne PKCE (`/api/oidc/callback`) — handles fresh login and session refresh on page load.
- **Agent connection**: `/api/copilotkit` proxies to the AG-UI endpoint, forwarding the `registry_agent_token` cookie as `x-agent-authorization`.
- **Dashboard state**: `registryRecords` and `metrics` are populated reactively as the agent calls tools. `clearDashboard()` resets both before a new query.
- **Tool renderers** (`useRenderTool`):
  - `read_schema` — renders a schema preview card (parses the fastmcp JSON-string envelope)
  - `read_relationships` — accumulates rows into the registry table + updates metric counters
  - `check_permission` — renders a ALLOWED / DENIED verdict card with subject/resource details
- **QuickActions**: trigger canned agent prompts via `agent.addMessage()` + `agent.runAgent()` from `useAgent()`.

### Registry Governor — `agent/`

Google ADK `LlmAgent` wrapped in an [ag_ui_adk](https://github.com/ag-ui-protocol/ag-ui) FastAPI server.

- Runs Gemini via direct API key (no Vertex AI needed — cluster is on AWS).
- `inject_mcp_auth` `before_agent_callback` reads the `x-agent-authorization` header (forwarded by the frontend), performs Exchange 2 (agent_token → mcp_token), and rebuilds an `McpToolset` for that turn.
- The MCP token is cached in-process (keyed on agent_token) to avoid redundant PingOne round-trips.
- Agent instructions enumerate valid permission/relation names from the schema to prevent hallucination.

### SpiceDB MCP Bridge — `k8s/mcp/`

[FastMCP](https://github.com/jlowin/fastmcp) server over streamable HTTP, wrapped in a Starlette app with bearer-token auth middleware.

- On startup, calls SpiceDB to read the live schema and extracts all `relation`/`permission` token names into `VALID_TOKENS`.
- Pydantic v2 models (`PermissionCheckArgs`, `RelationshipUpdateItem`) validate all inputs. The `permission` and `relation` fields are checked against `VALID_TOKENS`, rejecting invented names with a clear error.
- `resource_type` and `subject_type` are `Literal` types — FastMCP compiles these to an explicit enum in the JSON Schema sent to the LLM.
- `subject_id="me"` is resolved server-side from `X-Remote-Agent` / `X-Remote-User` headers injected by PingOne Advanced Services gateway.

---

## Configuration

### Secrets

`k8s/secrets.yaml` is excluded from version control. Create it with:

```bash
# SpiceDB preshared key + MCP API key
kubectl create secret generic spicedb-preshared-key \
  --namespace ping-devops-cprice \
  --from-literal=presharedKey="$(openssl rand -hex 32)" \
  --from-literal=mcpApiKey="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml > k8s/secrets.yaml

# Registry agent secrets
kubectl create secret generic registry-agent-secrets \
  --namespace ping-devops-cprice \
  --from-literal=GOOGLE_API_KEY="<gemini-api-key>" \
  --from-literal=MCP_BRIDGE_URL="https://notflux-registry-mcp.ping-devops.com/mcp" \
  --from-literal=PINGONE_ENV_ID="<pingone-env-id>" \
  --from-literal=PINGONE_CLIENT_ID="<client-id>" \
  --from-literal=PINGONE_CLIENT_SECRET="<client-secret>" \
  --from-literal=PINGONE_AGENT_AUDIENCE="<aud-claim-for-agent-token>" \
  --from-literal=PINGONE_MCP_SCOPE="<scope-that-targets-mcp-resource-server>" \
  --dry-run=client -o yaml >> k8s/secrets.yaml

# Frontend secrets
kubectl create secret generic registry-frontend-secrets \
  --namespace ping-devops-cprice \
  --from-literal=AGENT_ENGINE_URL="<ag-ui-endpoint>" \
  --from-literal=GOOGLE_GENERATIVE_AI_API_KEY="<key>" \
  --from-literal=PINGONE_ENV_ID="<pingone-env-id>" \
  --from-literal=PINGONE_CLIENT_ID_FRONTEND="<pkce-client-id>" \
  --from-literal=PINGONE_CLIENT_SECRET_FRONTEND="<pkce-client-secret>" \
  --from-literal=PINGONE_AGENT_SCOPE="registry-agent" \
  --from-literal=NEXT_PUBLIC_PINGONE_ENV_ID="<pingone-env-id>" \
  --from-literal=NEXT_PUBLIC_PINGONE_CLIENT_ID="<pkce-client-id>" \
  --dry-run=client -o yaml >> k8s/secrets.yaml
```

---

## Deployment

### Prerequisites

- Kubernetes cluster with `kubectl` configured
- `docker` with push access to your registry
- Secrets applied (see above)

### Bootstrap (first time)

```bash
chmod +x scripts/bootstrap.sh
./scripts/bootstrap.sh
```

### Build & deploy all three services

```bash
# Frontend
docker build -t docker.io/pricecs/notflux-registry-frontend:latest ./frontend
docker push docker.io/pricecs/notflux-registry-frontend:latest

# Agent
docker build -t docker.io/pricecs/registry-governor:latest ./agent
docker push docker.io/pricecs/registry-governor:latest

# MCP Bridge
docker build -t docker.io/pricecs/spicedb-mcp-bridge:latest ./k8s/mcp
docker push docker.io/pricecs/spicedb-mcp-bridge:latest

# Apply manifests
kubectl apply -f k8s/secrets.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/mcp-bridge.yaml
kubectl apply -f k8s/registry-agent.yaml
kubectl apply -f k8s/frontend.yaml

# Restart to pick up new images
kubectl rollout restart deployment/registry-frontend deployment/registry-agent deployment/spicedb-mcp-bridge \
  -n ping-devops-cprice
kubectl rollout status deployment/registry-frontend deployment/registry-agent deployment/spicedb-mcp-bridge \
  -n ping-devops-cprice --timeout=180s
```

### Endpoints (production)

| Service | URL |
|---|---|
| Dashboard | `https://notflux-registry.ping-devops.com` |
| MCP Bridge | `https://notflux-registry-mcp.ping-devops.com/mcp` |
| Agent (AG-UI) | Internal ClusterIP — accessed via frontend proxy |

---

## License

MIT

