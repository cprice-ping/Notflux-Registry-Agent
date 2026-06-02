# Agent Registry

A fine-grained authorization registry for agentic AI systems, built on [SpiceDB](https://github.com/authzed/spicedb).

Agents, capabilities, and namespaces are modeled as first-class resources with explicit relationship bindings. SpiceDB evaluates permission checks via a Zanzibar-compatible graph, ensuring zero-trust access control between agents, their owners, and the capabilities they are permitted to exercise.

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   Agent Registry                 │
│                                                  │
│  namespace ──► agent ──► capability              │
│      │            │                              │
│    admin         owner / viewer                  │
└──────────────────────────────────────────────────┘
         │
         ▼
     SpiceDB (gRPC + HTTP)
```

## Repository Structure

```
.
├── README.md
├── schema/
│   └── schema.zed           # The relationship graph definitions
├── k8s/
│   ├── secrets.yaml         # Preshared key for API authentication
│   ├── deployment.yaml      # SpiceDB service and runtime pod definitions
│   ├── patch-p1az.yaml      # HTTP Data Connector reference instructions
│   └── mcp-bridge.yaml      # MCP Bridge deployment, service and ingress
├── mcp/
│   ├── server.py            # FastMCP server (streamable HTTP transport)
│   ├── requirements.txt
│   └── Dockerfile
└── scripts/
    └── bootstrap.sh         # Convenience script to apply files
```

## Prerequisites

- A running Kubernetes cluster
- `kubectl` configured for the target cluster
- `zed` CLI (optional – for interactive schema management)

## Quick Start

```bash
# 1. Edit the preshared key in k8s/secrets.yaml before applying
# 2. Run the bootstrap script
chmod +x scripts/bootstrap.sh
./scripts/bootstrap.sh
```

## Schema

The SpiceDB schema lives in `schema/schema.zed`. It defines four resource types:

| Type | Description |
|---|---|
| `user` | A human principal (owner or viewer) |
| `agent` | An autonomous AI agent |
| `capability` | A discrete action or tool an agent may use |
| `namespace` | An organisational boundary grouping agents |

## Configuration

All tuneable values (replica count, image tag, resource limits) are commented inline in `k8s/deployment.yaml`.

The preshared key in `k8s/secrets.yaml` **must** be replaced with a securely generated value before deploying to any non-local environment:

```bash
kubectl create secret generic spicedb-preshared-key \
  --from-literal=presharedKey="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml > k8s/secrets.yaml
```

## License

MIT

---

## MCP Bridge – Natural Language Permission Management

The MCP Bridge is a [FastMCP](https://github.com/jlowin/fastmcp) server that
exposes SpiceDB as a set of MCP tools. Remote agents (including Google Vertex
AI Agent Builder) can call these tools over **streamable HTTP transport**
without any gRPC or SDK requirements.

### Connection details

| Field | Value |
|---|---|
| MCP endpoint | `https://notflux-registry-mcp.ping-devops.com/mcp` |
| Transport | `streamable-http` (MCP spec 2025-03-26) |
| Auth | `Authorization: Bearer <mcpApiKey>` |

The `mcpApiKey` is stored in the `spicedb-preshared-key` Kubernetes Secret.

### Available tools

| Tool | Purpose |
|---|---|
| `write_schema` | Overwrite the full SpiceDB permission schema |
| `update_relationships` | Create or delete agent → tool / server bindings |
| `check_permission` | Verify whether an agent may execute a specific tool |
| `read_schema` | Read the current schema text |
| `read_relationships` | Query existing relationships with optional filters |

---

### `write_schema`

Replaces the entire SpiceDB schema. Use when the permission model itself changes.

```json
{
  "tool": "write_schema",
  "arguments": {
    "schema": "definition user {}\ndefinition agent {\n    relation owner: user\n}\ndefinition mcp_server {\n    relation authorized_agent: agent\n}\ndefinition mcp_tool {\n    relation parent_server: mcp_server\n    relation direct_agent: agent\n    permission execute = direct_agent + parent_server->authorized_agent\n}"
  }
}
```

---

### `update_relationships`

Provisions or revokes `agent → tool` (or `agent → server`) permissions. This is
the primary tool for dynamic permission management.

**Grant a single tool to an agent** (`mcp_tool.direct_agent`):

```json
{
  "tool": "update_relationships",
  "arguments": {
    "updates": [
      {
        "operation":     "OPERATION_TOUCH",
        "resource_type": "mcp_tool",
        "resource_id":   "search_web",
        "relation":      "direct_agent",
        "subject_type":  "agent",
        "subject_id":    "agent-001"
      }
    ]
  }
}
```

**Grant an agent access to every tool on an MCP server** (`mcp_server.authorized_agent`):

```json
{
  "tool": "update_relationships",
  "arguments": {
    "updates": [
      {
        "operation":     "OPERATION_TOUCH",
        "resource_type": "mcp_server",
        "resource_id":   "research-server",
        "relation":      "authorized_agent",
        "subject_type":  "agent",
        "subject_id":    "agent-001"
      }
    ]
  }
}
```

**Revoke a permission** (swap `OPERATION_TOUCH` → `OPERATION_DELETE`):

```json
{
  "tool": "update_relationships",
  "arguments": {
    "updates": [
      {
        "operation":     "OPERATION_DELETE",
        "resource_type": "mcp_tool",
        "resource_id":   "search_web",
        "relation":      "direct_agent",
        "subject_type":  "agent",
        "subject_id":    "agent-001"
      }
    ]
  }
}
```

Multiple updates can be batched in a single call. SpiceDB applies them
atomically.

---

### Deploying the MCP Bridge

```bash
# 1. Generate and set the MCP API key in k8s/secrets.yaml
#    mcpApiKey: "$(openssl rand -hex 32)"

# 2. Build and push the container image
docker build -t <YOUR_REGISTRY>/spicedb-mcp-bridge:latest ./mcp
docker push <YOUR_REGISTRY>/spicedb-mcp-bridge:latest

# 3. Update the image: field in k8s/mcp-bridge.yaml, then apply
kubectl apply -f k8s/secrets.yaml
kubectl apply -f k8s/mcp-bridge.yaml
```
