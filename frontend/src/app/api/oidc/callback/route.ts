/**
 * POST /api/oidc/callback
 *
 * PingOne login bootstrap for the frontend.
 *
 * 1. Exchange the browser's authorization code for the person's access token.
 * 2. Exchange that person token for an agent audience token.
 * 3. Store the agent token in a same-origin cookie for /api/copilotkit.
 *
 * Body: { code: string, redirectUri: string, codeVerifier?: string }
 *    or { personToken: string }
 * Returns: { personToken: string }
 */

import { NextRequest, NextResponse } from "next/server";

const PINGONE_ENV_ID    = process.env.PINGONE_ENV_ID!;
const PINGONE_PUBLIC_CLIENT_ID = process.env.NEXT_PUBLIC_PINGONE_CLIENT_ID!;
const PINGONE_AGENT_CLIENT_ID = process.env.PINGONE_CLIENT_ID_FRONTEND!;
const PINGONE_AGENT_CLIENT_SECRET = process.env.PINGONE_CLIENT_SECRET_FRONTEND!;
const PINGONE_AGENT_SCOPE = process.env.PINGONE_AGENT_SCOPE ?? "registry-agent";

function makeBasicAuth(clientId: string, clientSecret: string): string {
  return Buffer.from(`${encodeURIComponent(clientId)}:${encodeURIComponent(clientSecret)}`).toString("base64");
}

async function fetchPingOneToken(params: Record<string, string>, authHeader?: string) {
  const resp = await fetch(`https://auth.pingone.com/${PINGONE_ENV_ID}/as/token`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      ...(authHeader ? { Authorization: authHeader } : {}),
    },
    body: new URLSearchParams(params),
  });

  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`PingOne token endpoint error (${resp.status}): ${body}`);
  }

  return await resp.json() as {
    access_token?: string;
    expires_in?: number;
  };
}

export async function POST(req: NextRequest) {
  let step = "step1:code-exchange";
  try {
    const { code, redirectUri, codeVerifier, personToken } = await req.json() as {
      code?: string;
      redirectUri?: string;
      codeVerifier?: string;
      personToken?: string;
    };

    let resolvedPersonToken = personToken ?? "";

    if (!resolvedPersonToken) {
      if (!code || !redirectUri) {
        return NextResponse.json({ error: "code and redirectUri required" }, { status: 400 });
      }

      console.info(`[oidc/callback] ${step} client_id=${PINGONE_PUBLIC_CLIENT_ID}`);
      const personTokenResponse = await fetchPingOneToken({
        grant_type: "authorization_code",
        client_id: PINGONE_PUBLIC_CLIENT_ID,
        code,
        redirect_uri: redirectUri,
        ...(codeVerifier ? { code_verifier: codeVerifier } : {}),
      });

      if (!personTokenResponse.access_token) {
        throw new Error("PingOne did not return a person access_token");
      }
      resolvedPersonToken = personTokenResponse.access_token;
      console.info(`[oidc/callback] ${step} ok`);
    } else {
      console.info("[oidc/callback] reusing existing person token to refresh agent session");
    }

    step = "step2:person->agent token-exchange";
    console.info(`[oidc/callback] ${step} client_id=${PINGONE_AGENT_CLIENT_ID} scope=${PINGONE_AGENT_SCOPE}`);
    const agentTokenResponse = await fetchPingOneToken(
      {
        grant_type: "urn:ietf:params:oauth:grant-type:token-exchange",
        subject_token: resolvedPersonToken,
        subject_token_type: "urn:ietf:params:oauth:token-type:access_token",
        requested_token_type: "urn:ietf:params:oauth:token-type:access_token",
        scope: PINGONE_AGENT_SCOPE,
      },
      `Basic ${makeBasicAuth(PINGONE_AGENT_CLIENT_ID, PINGONE_AGENT_CLIENT_SECRET)}`,
    );

    if (!agentTokenResponse.access_token) {
      throw new Error("PingOne did not return an agent access_token");
    }
    console.info(`[oidc/callback] ${step} ok`);

    const response = NextResponse.json({ personToken: resolvedPersonToken });
    response.cookies.set({
      name: "registry_agent_token",
      value: agentTokenResponse.access_token,
      httpOnly: true,
      sameSite: "lax",
      secure: true,
      path: "/",
      maxAge: Math.max(60, (agentTokenResponse.expires_in ?? 3600) - 30),
    });

    return response;
  } catch (err) {
    const msg = (err as Error).message ?? "Internal error";
    console.error(`[oidc/callback] FAILED at ${step}: ${msg}`);
    return NextResponse.json({ error: `${step}: ${msg}` }, { status: 500 });
  }
}
