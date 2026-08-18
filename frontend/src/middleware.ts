/**
 * Edge middleware — authenticates callers before any agent-facing route runs.
 *
 * WHY THIS EXISTS
 * The routes under `matcher` below proxy to the Registry Governor, which calls
 * Gemini on every run. They are reachable from the public internet on the
 * dashboard's hostname. The OIDC login in `page.tsx` is a *render* condition —
 * it decides what the browser paints and does not gate these routes, which are
 * separately addressable (`curl` never loads the page). Without a check here,
 * anyone who can reach the host can spend model quota on our credentials.
 *
 * WHAT IT CHECKS
 * The `registry_agent_token` cookie (or an `x-agent-authorization` header) must
 * be a PingOne-issued JWT that verifies against the environment's JWKS, with a
 * matching issuer and audience. Presence alone is not enough — an unverified
 * token would let any string through, and a token minted for a different app in
 * the same PingOne environment would otherwise be replayable here.
 *
 * WHAT IT DOES NOT DO
 * This is authentication, not authorization. It answers "is this a legitimate
 * caller?" so we don't spend money on strangers. Access-control decisions
 * (which agent may touch which resource) remain with P1AZ at the gateway and
 * the resource server.
 */
import { NextResponse, type NextRequest } from "next/server";
import { createRemoteJWKSet, jwtVerify } from "jose";

const ENV_ID = process.env.PINGONE_ENV_ID;
const AUDIENCE = process.env.PINGONE_AGENT_AUDIENCE;

const ISSUER = ENV_ID ? `https://auth.pingone.com/${ENV_ID}/as` : undefined;

// Cached across invocations; jose refreshes and rate-limits key fetches itself,
// so this does not hit PingOne per request.
const JWKS = ISSUER
  ? createRemoteJWKSet(new URL(`${ISSUER}/jwks`))
  : undefined;

function deny(reason: string, status = 401) {
  // The reason is logged, not returned — a caller learns only that it failed.
  console.warn(`[middleware] denied: ${reason}`);
  return NextResponse.json({ error: "authentication required" }, { status });
}

export async function middleware(req: NextRequest) {
  // Fail closed on missing configuration. Skipping validation when a variable
  // is unset would turn a deployment mistake into an open endpoint, which is
  // the exact failure this middleware exists to prevent.
  if (!ISSUER || !JWKS || !AUDIENCE) {
    return deny(
      "PINGONE_ENV_ID and/or PINGONE_AGENT_AUDIENCE not configured — refusing to serve",
      503,
    );
  }

  const raw =
    req.headers.get("x-agent-authorization") ??
    req.cookies.get("registry_agent_token")?.value ??
    "";
  const token = raw.replace(/^Bearer\s+/i, "").trim();

  if (!token) return deny("no credential presented");

  try {
    await jwtVerify(token, JWKS, { issuer: ISSUER, audience: AUDIENCE });
  } catch (err) {
    // Covers bad signature, expiry, wrong issuer, and wrong audience.
    return deny(`token rejected: ${(err as Error).message}`);
  }

  return NextResponse.next();
}

export const config = {
  // Only the routes that reach the agent. /api/oidc/callback is deliberately
  // absent — it is how a caller obtains a token in the first place, so gating
  // it would make login impossible.
  matcher: ["/api/copilotkit", "/api/mcp-servers"],
};
