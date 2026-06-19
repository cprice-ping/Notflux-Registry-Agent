import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_ENGINE_URL ?? "http://localhost:8000";

export async function GET() {
  // Forward the agent token so the probe can perform a real Exchange 2
  // and hit the gateway with a valid token — status reflects actual policy.
  const cookieStore = await cookies();
  const agentToken = cookieStore.get("registry_agent_token")?.value;

  const headers: Record<string, string> = {};
  if (agentToken) headers["x-agent-authorization"] = `Bearer ${agentToken}`;

  const resp = await fetch(`${AGENT_URL}/mcp-servers`, {
    cache: "no-store",
    headers,
  });
  if (!resp.ok) {
    return NextResponse.json({ error: "upstream error" }, { status: resp.status });
  }
  const data = await resp.json();
  return NextResponse.json(data);
}
