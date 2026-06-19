import { NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_ENGINE_URL ?? "http://localhost:8000";

export async function GET() {
  const resp = await fetch(`${AGENT_URL}/mcp-servers`, { cache: "no-store" });
  if (!resp.ok) {
    return NextResponse.json({ error: "upstream error" }, { status: resp.status });
  }
  const data = await resp.json();
  return NextResponse.json(data);
}
