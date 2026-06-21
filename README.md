# Agent Registry

A fine-grained authorization registry for agentic AI systems, built on [SpiceDB](https://github.com/authzed/spicedb).

Agents and MCP servers are modeled as first-class resources. An ADK-based governor agent manages the registry through natural language, while a Next.js dashboard gives administrators live visibility and Human-in-the-Loop control over every permission change.

---

## Architecture

```
 Browser
 ┌──────────────────────────────────────────────────────────────────┐
 │  Next.js Dashboard  (CopilotKit v2)                             │
 │  • Reactive relationship table & metrics cards                  │
 │  • Inline tool renderers (schema, relationships, checks)        │
 │  • PingOne PKCE login + token exchange                          │
 └─────────────────────────┬────────────────────────────────────────┘
                           │  AG-UI (CopilotKit protocol)
                           ▼
          ┌────────────────────────────────┐
          │  Registry Governor             │
          │  (Google ADK + ag_ui_adk)      │
          │  LlmAgent (Gemini)             │
          │  Per-turn MCP token exchange   │
          └──────────┬─────────────────────┘
                     │  MCP (two toolsets per turn)
          ┌──────────┴─────────────┐
          ▼                        ▼
 ┌─────────────────────┐  ┌──────────────────────────────┐
 │ SpiceDB MCP Bridge  │  │ Registry PIP  (FastMCP)      │
 │ (FastMCP+Starlette) │  │ • register_entity            │
 │ • read/write_schema │  │ • resolve_entity             │
 │ • update_relations  │  │ • list_entities              │
 │ • check_permission  │  │ • find_entity_by_name        │
 │ • read_relations    │  │ Static Bearer token auth     │
 │ Live schema valid.  │  └──────────┬───────────────────┘
 └──────────┬──────────┘             │  SQLAlchemy async
            │  SpiceDB REST API      ▼
            ▼               ┌────────────────────┐
  ┌──────────────────┐      │  PostgreSQL 16      │
  │  SpiceDB         │      │  entity name→ID     │
  │  Zanzibar-compat │      └────────┬────────────┘
  │  permission graph│               │  GET /v1/entities
  └──────────────────┘               ▼
                           ┌──────────────────────┐
                           │  Kong (db-less)       │
                           │  ping-auth on /v1     │
                           │  → P1AZ Hybrid GW     │
                           └──────────────────────┘
```

**Auth gateway split:**
- SpiceDB MCP Bridge is fronted by **PingGateway** (PingOne Advanced Services) — handles per-turn token exchange for the Governor agent.
- Registry PIP REST (`/v1`) is fronted by **Kong** with the `ping-auth` plugin wired to a **PingOne Authorize Hybrid Gateway**, giving P1AZ ABAC control over entity resolution.
- Registry PIP MCP (`/mcp`) uses a static bearer token validated in-process — no gateway needed for agent-to-agent calls.

All components run in the `ping-devops-cprice` Kubernetes namespace.

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
│   ├── agent.py                # LlmAgent + token exchange + two MCP toolsets
│   ├── server.py               # AG-UI FastAPI server entry point
│   ├── requirements.txt
│   └── Dockerfile
├── mcp/                        # SpiceDB MCP Bridge source
│   ├── server.py               # FastMCP + Starlette + Pydantic v2 validation
│   ├── requirements.txt
│   └── Dockerfile
├── registry_service/           # Registry PIP microservice
│   ├── app/
│   │   ├── main.py             # Combined Starlette ASGI app (/mcp + /v1 + /healthz)
│   │   ├── mcp_server.py       # FastMCP tools (register, resolve, list, find)
│   │   ├── rest_api.py         # FastAPI REST router (GET /v1/entities)
│   │   ├── models.py           # SQLAlchemy Entity ORM model
│   │   ├── schemas.py          # Pydantic response schemas
│   │   └── database.py         # Async SQLAlchemy engine + session factory
│   ├── requirements.txt
│   └── Dockerfile
├── k8s/
│   ├── deployment.yaml         # SpiceDB deployment + ClusterIP service
│   ├── frontend.yaml           # Frontend deployment, service, ingress
│   ├── registry-agent.yaml     # Agent deployment + ClusterIP service (no public ingress)
│   ├── mcp-bridge.yaml         # MCP Bridge deployment, service, ingress
│   ├── registry-pip.yaml       # Registry PIP deployment, service, ingress
│   ├── registry-pip-postgres.yaml  # In-cluster PostgreSQL StatefulSet
│   ├── patch-p1az.yaml         # PingOne Advanced Services gateway patch
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
  ├── owner: user                      — which human owns this agent
  └── active_driver: user              — ephemeral driver, supplied via P1AZ contextual tuples

mcp_server
  ├── authorized_agent: agent          — agent has blanket access to all tools on this server
  ├── authorized_user: user            — user can view this server
  ├── public_to_all_users: user:*      — open to all authenticated users
  ├── view_server (permission)      = authorized_user + public_to_all_users
  └── agent_can_connect (permission) = authorized_agent

mcp_tool
  ├── parent_server: mcp_server        — which server this tool belongs to
  ├── direct_agent: agent              — agent has direct access to this specific tool
  └── execute (permission) = (direct_agent + parent_server->authorized_agent)
                             & parent_server->view_server
```

An agent can execute a tool when it has **either** a `direct_agent` relationship on the
tool **or** an `authorized_agent` relationship on the tool's parent server — **and** the
parent server is viewable (`view_server`). The `view_server` conjunct ensures a tool is
never executable on a server the caller cannot see.

---

## Auth & Token Flow

The system uses PingOne as its identity provider with a three-hop token exchange:

```
1. Browser                  2. Frontend (server-side)       3. Agent (per-turn)
───────────────────         ──────────────────────────      ────────────────────────────────
PKCE login (PingOne)   →    Exchange 1:                 →   DaVinci Exchange (RFC 8693):
  id_token / code           person_token                    subject_token = agent_token
                            (aud = registry-person)         actor_token   = k8s SA token
                                  │                           (projected from pod volume)
                            Exchange 1b:                    ↓
                            agent_token                     DaVinci policy flow:
                            (aud = registry-agent)          • validates k8s OIDC JWT
                            stored as httpOnly cookie        • hashes actor sub → sub_hash
                            registry_agent_token            • mints mcp_token with:
                                                              aud  = gateway MCP endpoint
                                                              scope = use_gateway
                                                              act.sub      = raw k8s sub
                                                              act.sub_hash = sha256(sub)
                                                            ↓
                                                          McpToolset per turn
                                                          Authorization: Bearer mcp_token
```

**Key details:**
- The `registry_agent_token` cookie is set by `/api/oidc/callback` on every login and on every page load for cached sessions (the frontend re-runs Exchange 1b on mount if a `registry_person_token` is already in sessionStorage).
- The Governor agent reads its k8s Service Account token from `/var/run/secrets/tokens/davinci-token` (projected volume, kubelet-rotated, audience `davinci-sts`) and passes it as `actor_token` to the DaVinci policy flow.
- DaVinci hashes `actor_token.sub` (SHA-256 hex) and embeds it as `act.sub_hash` in the issued `mcp_token`. This is the canonical workload identifier used in SpiceDB tuples and P1AZ policy decisions — the raw sub (which contains colons or slashes) is preserved as `act.sub` for audit but never used as a SpiceDB object ID.
- **When PingOne natively supports third-party `actor_token` (RFC 8693):** replace the DaVinci flow with a PingOne token policy that uses PEL `${#crypto.sha256Hex(actor.sub)}` to compute the same hash. The `act.sub_hash` claim shape stays identical — no downstream changes needed.

**Delegation semantics (RFC 8693):** the exchanged token encodes proper delegation —
`sub` is the **human** who delegated, and the `act` (actor) claim is the **agent** acting
on their behalf (`act.sub` raw for audit, `act.sub_hash` as the SpiceDB-safe agent id).

### Where authorization decisions are made

All access-control decisions are made by **P1AZ — at the gateway and at the resource
server**:
- **PingGateway** (PingOne Advanced Services) validates the inbound token, runs the
  per-turn token exchange, and applies policy before forwarding the request.
- **Kong + PingOne Authorize Hybrid Gateway** evaluates ABAC policy on the Registry PIP
  REST surface.

The Governor agent and the MCP servers (SpiceDB bridge, Registry PIP) are **deliberately
thin executors** — they carry out instructions and never make authorization decisions
themselves. The agent's prompt rules are UX guardrails, not a security boundary.

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
- `inject_mcp_auth` `before_agent_callback` reads the `x-agent-authorization` header, performs the DaVinci token exchange (agent_token + k8s SA actor_token → mcp_token), then rebuilds **three** `McpToolset` instances for that turn:
  1. **SpiceDB MCP Bridge** — authenticated with the per-turn exchanged PingOne token (via PingGateway).
  2. **Weather MCP server** — same per-turn token, same gateway.
  3. **Registry PIP** — authenticated with a static `REGISTRY_PIP_API_KEY` bearer token (no per-turn exchange).
- The k8s SA token is read fresh from disk on each cache-miss (kubelet rotates it; the file is tmpfs so reads are cheap).
- The mcp_token is cached in-process (keyed on agent_token) to avoid redundant DaVinci round-trips.
- Agent instructions enumerate valid permission/relation names and describe the two-step onboarding workflow.
- When an admin refers to an entity by name, the agent calls `find_entity_by_name` first to resolve the ID before calling any SpiceDB tools.

### SpiceDB MCP Bridge — `mcp/`

[FastMCP](https://github.com/jlowin/fastmcp) server over streamable HTTP, wrapped in a Starlette app with bearer-token auth middleware.

- On startup, calls SpiceDB to read the live schema and extracts all `relation`/`permission` token names into `VALID_TOKENS`.
- Pydantic v2 models (`PermissionCheckArgs`, `RelationshipUpdateItem`) validate all inputs. The `permission` and `relation` fields are checked against `VALID_TOKENS`, rejecting invented names with a clear error.
- `resource_type` and `subject_type` are `Literal` types — FastMCP compiles these to an explicit enum in the JSON Schema sent to the LLM.
- **The bridge is a pure executor — it makes no authorization decisions.** Access is decided upstream by P1AZ (at the PingGateway and at the resource server) before a request ever reaches the bridge; the bridge validates input shape and carries out the requested SpiceDB read/write. Delegated identity travels in the exchanged token claims (`sub` = the human delegator, `act.sub` / `act.sub_hash` = the agent actor), not in request headers.

### Registry PIP — `registry_service/`

FastMCP + FastAPI microservice backed by PostgreSQL. The **name-to-ID source of truth** for all entities in the cluster.

**MCP tools** (consumed by the Governor agent and any MCP client):

| Tool | Purpose |
|---|---|
| `register_entity(id, type, name, owner_guid, metadata?, sub?)` | Upsert an entity. Pass `sub` for workload identities (k8s SA, Vertex Agent) — returns `sub_hash` to use in SpiceDB tuples. |
| `resolve_entity(id)` | Look up a single entity by its stable ID. Returns `sub_hash` if set. |
| `list_entities(type?)` | Browse all registered entities, optionally filtered by type. |
| `find_entity_by_name(name, type?)` | Case-insensitive substring search by human-readable name. Breaks the ID catch-22 — ask by name, get the ID back. |
| `delete_entity(id)` | Permanently remove a Registry record. Always remove SpiceDB relationships first. |

**REST endpoints** (consumed by P1AZ during authorization decisions):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/entities/{id}` | Resolve a single entity by ID (high-frequency PK lookup for P1AZ). |
| `GET` | `/v1/entities?type=agent&name=notflux` | List / search entities by type and/or name substring. |
| `GET` | `/healthz` | Unauthenticated liveness check. |

The `/mcp` path uses static bearer token auth validated in-process. The `/v1` path is fronted by Kong + PingOne Authorize.

### Kong + PingOne Authorize Hybrid Gateway

Kong (db-less, custom image with `ping-auth` plugin) fronts the Registry PIP REST interface.

- Two declarative routes: `/v1` (with `ping-auth` plugin) and `/mcp` (no plugin — bearer token handled in-process).
- The `ping-auth` plugin is **scoped to the `/v1` route only** so MCP streaming is not interrupted.
- The PingOne Authorize Hybrid Gateway connects to PingOne env `NotFlux` and evaluates ABAC policies on REST requests.
- Kong deployed as `notflux-registry-api-kong` via Helm (declarative ConfigMap). Hybrid Gateway deployed as `notflux-registry-api-p1az-gateway`.

---

## Configuration

### Secrets

`k8s/secrets.yaml` is excluded from version control. Create each secret with `kubectl create secret`.

```bash
# SpiceDB preshared key + MCP bridge bearer token
kubectl create secret generic spicedb-preshared-key \
  --namespace ping-devops-cprice \
  --from-literal=presharedKey="$(openssl rand -hex 32)" \
  --from-literal=mcpApiKey="$(openssl rand -hex 32)"

# Registry PIP — Postgres credentials
kubectl create secret generic registry-pip-postgres-secrets \
  --namespace ping-devops-cprice \
  --from-literal=POSTGRES_USER="registry_pip" \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  --from-literal=POSTGRES_DB="registry"

# Registry PIP — app secrets (use the password set above)
kubectl create secret generic registry-pip-secrets \
  --namespace ping-devops-cprice \
  --from-literal=DATABASE_URL="postgresql+asyncpg://registry_pip:<password>@registry-postgres.ping-devops-cprice.svc.cluster.local:5432/registry" \
  --from-literal=MCP_API_KEY="$(openssl rand -hex 32)"

# Registry Agent secrets
# REGISTRY_PIP_API_KEY: copy from registry-pip-secrets MCP_API_KEY
# NOTE: the token exchange runs through the DaVinci flow, so the legacy
# PINGONE_ENV_ID / PINGONE_CLIENT_ID / PINGONE_CLIENT_SECRET / PINGONE_MCP_SCOPE
# keys are no longer consumed by the agent and have been dropped.
kubectl create secret generic registry-agent-secrets \
  --namespace ping-devops-cprice \
  --from-literal=GOOGLE_API_KEY="<gemini-api-key>" \
  --from-literal=MCP_BRIDGE_URL="https://<your-gateway-host>/mcp/agent-registry" \
  --from-literal=WEATHER_MCP_URL="https://<your-gateway-host>/mcp/weather" \
  --from-literal=PINGONE_AGENT_AUDIENCE="<aud-claim-for-agent-token>" \
  --from-literal=DAVINCI_POLICY_URL="https://orchestrate-api.pingone.com/v1/company/<env-id>/policy/<flow-id>/start" \
  --from-literal=DAVINCI_POLICY_API_KEY="<davinci-flow-api-key>" \
  --from-literal=REGISTRY_PIP_URL="https://<registry-pip-host>/mcp" \
  --from-literal=REGISTRY_PIP_API_KEY="<pip-mcp-api-key>"

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
  --from-literal=NEXT_PUBLIC_PINGONE_CLIENT_ID="<pkce-client-id>"
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

### Build & deploy all services

```bash
# Frontend
docker build -t docker.io/pricecs/notflux-registry-frontend:latest ./frontend
docker push docker.io/pricecs/notflux-registry-frontend:latest

# Registry Governor agent
docker build -t docker.io/pricecs/registry-governor:latest ./agent
docker push docker.io/pricecs/registry-governor:latest

# SpiceDB MCP Bridge
docker build -t docker.io/pricecs/spicedb-mcp-bridge:latest ./mcp
docker push docker.io/pricecs/spicedb-mcp-bridge:latest

# Registry PIP
docker build -t docker.io/pricecs/registry-pip:latest ./registry_service
docker push docker.io/pricecs/registry-pip:latest

# Apply manifests (Postgres must be Running before PIP)
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/registry-pip-postgres.yaml
kubectl apply -f k8s/registry-pip.yaml
kubectl apply -f k8s/mcp-bridge.yaml
kubectl apply -f k8s/registry-agent.yaml
kubectl apply -f k8s/frontend.yaml

# Restart deployments to pick up new images
kubectl rollout restart \
  deployment/registry-frontend \
  deployment/registry-agent \
  deployment/spicedb-mcp-bridge \
  deployment/registry-pip \
  -n ping-devops-cprice
kubectl rollout status \
  deployment/registry-frontend \
  deployment/registry-agent \
  deployment/spicedb-mcp-bridge \
  deployment/registry-pip \
  -n ping-devops-cprice --timeout=180s
```

### Endpoints (production)

| Service | URL | Auth |
|---|---|---|
| Dashboard | `https://notflux-registry.ping-devops.com` | PingOne PKCE |
| SpiceDB MCP Bridge | `https://notflux-registry.notflux-priv-gateway.ping-devops.com/mcp` | PingGateway token exchange |
| Registry PIP (MCP) | `https://notflux-registry-pip.ping-devops.com/mcp` | Static Bearer (`REGISTRY_PIP_API_KEY`) |
| Registry PIP (REST) | `https://notflux-registry-api.ping-devops.com/v1/entities` | Kong → PingOne Authorize |
| Agent (AG-UI) | Internal ClusterIP | Via frontend `/api/copilotkit` proxy |

---

## Entity Onboarding Workflow

Every new agent, user, or MCP server must be registered in **both** systems to be fully operational.

### Standard entities (PingOne GUID or opaque slug)

```
STEP 1 — Register in Registry PIP:
  register_entity(
    id="<stable-resource-id>",
    type="agent",
    name="My Agent Display Name",
    owner_guid="<owner-pingone-guid>"
  )

STEP 2 — Grant permissions in SpiceDB (use the SAME id as subject_id):
  update_relationships([{
    "resource_type": "agent",  "resource_id": "<id>",
    "relation":      "owner",  "subject_type": "user",
    "subject_id":    "<id>"
  }], operation="OPERATION_TOUCH")
```

### Workload identities (k8s Service Account, Vertex Agent)

OIDC `sub` claims from workload identities contain characters illegal in SpiceDB object IDs — k8s uses colons (`system:serviceaccount:namespace:name`) and Vertex AI uses slashes (`projects/x/locations/y/agents/z`). SpiceDB only permits alphanumerics, hyphens, underscores, and dots.

Rather than slugging or truncating the sub (which risks collisions and loses the full identity), we SHA-256 hash it. The hash is:
- **Character-safe** — 64 hex chars, always valid as a SpiceDB object ID
- **Collision-resistant** — any two distinct subs produce distinct hashes with overwhelming probability
- **Consistent** — `hashlib.sha256(sub.encode()).hexdigest()` in Python, `${#crypto.sha256Hex(actor.sub)}` in PEL, same output
- **Auditable** — the raw sub is stored in Registry PIP's `raw_sub` column and preserved as `act.sub` in the token; the hash is `act.sub_hash`

P1AZ reads `act.sub_hash` from the mcp_token for SpiceDB permission checks. The raw sub is never used as a SpiceDB identifier.

```
STEP 1 — Register with the raw sub:
  register_entity(
    id="notflux-registry-agent",
    type="agent",
    name="NotFlux Registry Agent",
    owner_guid="<owner-pingone-guid>",
    sub="system:serviceaccount:ping-devops-cprice:notflux-registry-agent"
    #   ↑ raw OIDC sub — may contain colons or slashes
  )
  → Response includes: sub_hash=<64-hex-chars>
    Record this value before proceeding.

STEP 2 — Grant permissions using sub_hash (NOT the raw sub, NOT the id):
  update_relationships([{
    "resource_type": "agent",  "resource_id": "notflux-registry-agent",
    "relation":      "owner",  "subject_type": "agent",
    "subject_id":    "<sub_hash from Step 1>"
  }], operation="OPERATION_TOUCH")
```

The `sub_hash` is SHA-256 hex of the raw sub — the same value DaVinci embeds as `act.sub_hash` in the mcp_token. P1AZ reads `act.sub_hash` from the token and matches it against the SpiceDB tuple.

### Deletion workflow

Always clean up SpiceDB **before** deleting the Registry record (orphaned tuples can't be resolved by name afterward):

```
STEP 1 — Read existing SpiceDB relationships.
STEP 2 — Delete all tuples (including ones where entity is subject, not resource).
STEP 3 — delete_entity(id="<id>").
STEP 4 — Re-register if needed (follow onboarding above).
```

> An entity in SpiceDB but not in Registry PIP has permissions but no resolvable name.
> An entity in Registry PIP but not SpiceDB has a name but no access grants.
> Both are needed for a fully functional, auditable entity.

---

## License

MIT

