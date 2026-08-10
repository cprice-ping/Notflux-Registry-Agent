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

/**
 * Strip Gemini code-execution markup from AG-UI TEXT_MESSAGE_CONTENT deltas.
 *
 * Gemini 2.5 Flash emits tool calls as <tool_code>...</tool_code> and results
 * as <tool_response>...</tool_response> inside the plain-text stream.  These
 * are internal artefacts and should never surface in the chat UI.
 *
 * The filter is stateful: tags may span multiple SSE chunks, so we track
 * whether we are currently inside a suppressed block.
 */
function filterGeminiToolMarkup(upstream: Response): Response {
  if (!upstream.body) return upstream;

  const OPEN_TAGS  = ["<tool_code>", "<tool_response>"];
  const CLOSE_TAGS = ["</tool_code>", "</tool_response>"];

  let depth   = 0;   // >0 means we are inside a suppressed block
  let buf     = "";  // carries partial tag text across chunk boundaries

  const transform = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      const decoder = new TextDecoder();
      const encoder = new TextEncoder();
      const text = decoder.decode(chunk, { stream: true });

      let out = "";
      let pos = 0;

      while (pos < text.length) {
        if (depth === 0) {
          // Outside a suppressed block: look for an opening tag.
          const combined = buf + text.slice(pos);
          let earliest = -1;
          let matchTag = "";
          for (const tag of OPEN_TAGS) {
            const idx = combined.indexOf(tag);
            if (idx !== -1 && (earliest === -1 || idx < earliest)) {
              earliest = idx; matchTag = tag;
            }
          }
          if (earliest !== -1) {
            // Emit everything before the tag, then enter suppressed mode.
            out += combined.slice(0, earliest);
            buf  = "";
            pos  = text.length - (combined.length - earliest - matchTag.length);
            depth++;
          } else {
            // No opening tag found — but it might be split across the boundary.
            // Keep a tail of (max tag length - 1) chars in the buffer.
            const tail = Math.max(0, combined.length - 20);
            out += combined.slice(0, tail);
            buf  = combined.slice(tail);
            pos  = text.length;
          }
        } else {
          // Inside a suppressed block: scan for the matching close tag.
          const combined = buf + text.slice(pos);
          let earliest = -1;
          let matchTag = "";
          for (const tag of CLOSE_TAGS) {
            const idx = combined.indexOf(tag);
            if (idx !== -1 && (earliest === -1 || idx < earliest)) {
              earliest = idx; matchTag = tag;
            }
          }
          if (earliest !== -1) {
            buf  = "";
            pos  = text.length - (combined.length - earliest - matchTag.length);
            depth--;
          } else {
            buf = combined.slice(Math.max(0, combined.length - 20));
            pos = text.length;
          }
        }
      }

      // Only rewrite TEXT_MESSAGE_CONTENT events; pass others through as-is.
      // The rewrite replaces the "delta" value with the filtered text.
      if (out) {
        const rewritten = out.replace(
          /("type"\s*:\s*"TEXT_MESSAGE_CONTENT"[^}]*"delta"\s*:\s*)"((?:[^"\\]|\\.)*)"/g,
          (match, prefix, delta) => {
            // Decode JSON-escaped string, strip any residual tag fragments, re-encode.
            const decoded = delta.replace(/\\n/g, "\n").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
            const filtered = decoded.replace(/<\/?tool_(?:code|response)>/g, "").trimEnd();
            if (!filtered) return match.replace(prefix, `"_type_suppress":"TEXT_MESSAGE_CONTENT","_delta_suppress":`);
            const encoded  = filtered.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n");
            return `${prefix}"${encoded}"`;
          },
        );
        controller.enqueue(encoder.encode(rewritten));
      }
    },
    flush(controller) {
      // Emit anything left in the buffer that wasn't suppressed.
      if (buf && depth === 0) {
        controller.enqueue(new TextEncoder().encode(buf));
      }
    },
  });

  return new Response(upstream.body.pipeThrough(transform), {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") ?? "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}

function streamResponse(upstream: Response) {
  return filterGeminiToolMarkup(upstream);
}

async function forwardToAgent(req: NextRequest, payload: unknown) {
  const agentAuth = req.headers.get("x-agent-authorization") ?? req.cookies.get("registry_agent_token")?.value ?? "";
  console.info("[copilotkit] forwarding auth", {
    headerPresent: Boolean(req.headers.get("x-agent-authorization")),
    cookiePresent: Boolean(req.cookies.get("registry_agent_token")?.value),
  });

  // Require a session before invoking the agent. Without this the route is an
  // open relay to a Gemini-backed agent loop: an unauthenticated caller can burn
  // LLM quota (the agent runs without MCP tools but still calls the model).
  // This is authentication — may this caller spend LLM budget — not an
  // authorization decision; access policy still belongs to P1AZ.
  if (!agentAuth) {
    return NextResponse.json({ error: "authentication required" }, { status: 401 });
  }

  const upstream = await fetch(AGENT_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Accept": req.headers.get("accept") ?? "text/event-stream",
      "x-agent-authorization": agentAuth.startsWith("Bearer ") ? agentAuth : `Bearer ${agentAuth}`,
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

