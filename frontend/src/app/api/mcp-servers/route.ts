/**
 * GET /api/mcp-servers
 *
 * Proxies to the agent's /mcp-servers endpoint, forwarding the agent token
 * so the agent can perform Exchange 2 (agent_token → mcp_token) with its own
 * PingOne credentials and probe each MCP server directly.
 */
import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_ENGINE_URL ?? "http://localhost:8000";

export async function GET() {
  const cookieStore = await cookies();
  const agentToken  = cookieStore.get("registry_agent_token")?.value;

  const headers: Record<string, string> = {};
  if (agentToken) headers["x-agent-authorization"] = `Bearer ${agentToken}`;

  try {
    const resp = await fetch(`${AGENT_URL}/mcp-servers`, {
      cache: "no-store",
      headers,
    });
    if (!resp.ok) {
      return NextResponse.json({ error: `upstream_status_${resp.status}` }, { status: resp.status });
    }
    return NextResponse.json(await resp.json());
  } catch {
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }
}
