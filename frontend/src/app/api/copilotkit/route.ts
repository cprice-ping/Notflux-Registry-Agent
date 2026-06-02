/**
 * POST /api/copilotkit
 *
 * CopilotKit single-endpoint runtime shim. CopilotKit expects a runtime that
 * responds to JSON envelopes like { method: "info" } and
 * { method: "agent/run", params, body }.
 *
 * The actual agent is an ag_ui_adk server, so this route advertises minimal
 * runtime metadata and unwraps run/connect envelopes before forwarding the raw
 * AG-UI RunAgentInput body to the agent.
 */
import { NextRequest, NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_ENGINE_URL!;

type SingleEndpointEnvelope = {
  method?: string;
  params?: {
    agentId?: string;
    threadId?: string;
  };
  body?: unknown;
};

const runtimeInfo = {
  version: "1.0.0",
  agents: {
    default: {
      name: "default",
      description: "Registry Governor for Notflux agent permissions.",
      className: "ADKAgent",
    },
  },
  audioFileTranscriptionEnabled: false,
  mode: "sse",
  a2uiEnabled: false,
  openGenerativeUIEnabled: false,
  telemetryDisabled: true,
};

function streamResponse(upstream: Response) {
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") ?? "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}

async function forwardToAgent(req: NextRequest, payload: unknown) {
  const agentAuth = req.headers.get("x-agent-authorization") ?? req.cookies.get("registry_agent_token")?.value ?? "";
  console.info("[copilotkit] forwarding auth", {
    headerPresent: Boolean(req.headers.get("x-agent-authorization")),
    cookiePresent: Boolean(req.cookies.get("registry_agent_token")?.value),
  });

  const upstream = await fetch(AGENT_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Accept": req.headers.get("accept") ?? "text/event-stream",
      ...(agentAuth ? { "x-agent-authorization": agentAuth.startsWith("Bearer ") ? agentAuth : `Bearer ${agentAuth}` } : {}),
    },
    body: JSON.stringify(payload),
    // @ts-expect-error Node fetch accepts duplex for streamed request/response handling.
    duplex: "half",
  });

  return streamResponse(upstream);
}

export const POST = async (req: NextRequest) => {
  let envelope: SingleEndpointEnvelope;

  try {
    envelope = await req.json() as SingleEndpointEnvelope;
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  switch (envelope.method) {
    case "info":
      return NextResponse.json(runtimeInfo);

    case "agent/run":
    case "agent/connect":
      if (!envelope.body || typeof envelope.body !== "object") {
        return NextResponse.json({ error: "agent body required" }, { status: 400 });
      }
      return forwardToAgent(req, envelope.body);

    case "agent/stop":
      return new Response(null, { status: 204 });

    default:
      return NextResponse.json(
        { error: `unsupported method: ${String(envelope.method ?? "")}` },
        { status: 400 },
      );
  }
};

