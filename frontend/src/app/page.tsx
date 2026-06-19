"use client";

import "@copilotkit/react-core/v2/styles.css";
import {
  CopilotKit,
  CopilotSidebar,
  useRenderTool,
  useAgent,
} from "@copilotkit/react-core/v2";
import { useCopilotReadable } from "@copilotkit/react-core";
import { useEffect, useState, Fragment } from "react";
import { z } from "zod";

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

function generateVerifier(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(48));
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
}

async function deriveChallenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return btoa(String.fromCharCode(...new Uint8Array(hash)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
}

async function redirectToLogin() {
  const verifier  = generateVerifier();
  const challenge = await deriveChallenge(verifier);
  sessionStorage.setItem("pkce_verifier", verifier);

  const params = new URLSearchParams({
    response_type:         "code",
    client_id:             process.env.NEXT_PUBLIC_PINGONE_CLIENT_ID ?? "",
    redirect_uri:          `${window.location.origin}/`,
    scope:                 "openid profile email",
    state:                 crypto.randomUUID(),
    code_challenge:        challenge,
    code_challenge_method: "S256",
  });
  window.location.href =
    `https://auth.pingone.com/${process.env.NEXT_PUBLIC_PINGONE_ENV_ID}/as/authorize?${params}`;
}

async function exchangeCode(code: string): Promise<string> {
  const codeVerifier = sessionStorage.getItem("pkce_verifier") ?? "";
  sessionStorage.removeItem("pkce_verifier");

  const resp = await fetch("/api/oidc/callback", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, redirectUri: window.location.origin + "/", codeVerifier }),
  });
  if (!resp.ok) throw new Error("OIDC code exchange failed");
  const data = await resp.json();
  return data.personToken as string;
}

async function refreshAgentSession(personToken: string): Promise<void> {
  const resp = await fetch("/api/oidc/callback", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ personToken }),
  });
  if (!resp.ok) throw new Error("Agent session refresh failed");
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Metrics {
  activeAgents: number | null;
  mcpServers: number | null;
  pendingGrants: number | null;
}

interface McpServer {
  name: string;
  url: string;
  auth: string;
  tools: string[];
  reachable?: boolean;
}

interface RelationshipRow {
  resource: string;
  relation: string;
  subject: string;
}

// Normalise a SpiceDB ref — handles all known wire formats:
//   plain string "mcp_server:foo"
//   {objectType, objectId}         — SpiceDB resource ref
//   {object: {objectType, objectId}} — SpiceDB subject ref
//   {type, id}                     — alternate form
function normalizeRef(v: unknown): string {
  if (typeof v === "string") return v;
  if (typeof v === "object" && v !== null) {
    const o = v as Record<string, unknown>;
    if (o.objectType && o.objectId) return `${o.objectType}:${o.objectId}`;
    if (o.type && o.id)             return `${o.type}:${o.id}`;
    if (o.object)                   return normalizeRef(o.object);
  }
  return String(v ?? "");
}

// Normalise one raw item from the results array — handles:
//   {resource, relation, subject}            — flat
//   {relationship: {resource, relation, subject}} — SpiceDB ReadRelationships wrapper
function normalizeItem(item: unknown): RelationshipRow | null {
  if (typeof item !== "object" || item === null) return null;
  const r = item as Record<string, unknown>;
  // Unwrap SpiceDB ReadRelationshipsResponse wrapper
  const rel = (r.relationship as Record<string, unknown>) ?? r;
  const resource = normalizeRef(rel.resource ?? rel.resourceType);
  const subject  = normalizeRef(rel.subject  ?? rel.subjectType);
  const relation = typeof rel.relation === "string" ? rel.relation : String(rel.relation ?? "");
  if (!resource && !subject) return null;
  return { resource, relation, subject };
}

function parseRelationships(result: unknown): RelationshipRow[] {
  if (!result) return [];

  let raw: unknown;
  try { raw = typeof result === "string" ? JSON.parse(result) : result; }
  catch { return []; }

  // Unwrap fastmcp envelope: {structuredContent: {result: [...]}, content: [{text: ...}]}
  if (typeof raw === "object" && raw !== null) {
    const o = raw as Record<string, unknown>;
    const sc = o.structuredContent as Record<string, unknown> | undefined;
    if (sc && Array.isArray(sc.result)) {
      raw = sc.result;
    } else if (Array.isArray(o.content)) {
      // Fall back to content[0].text which is a JSON string
      const first = (o.content as Record<string, unknown>[])[0];
      if (typeof first?.text === "string") {
        try { raw = JSON.parse(first.text); } catch { /* ignore */ }
      }
    }
  }

  // Collect into a flat string/object array
  let arr: unknown[] = [];
  if (Array.isArray(raw)) {
    arr = raw;
  } else if (typeof raw === "object" && raw !== null) {
    const o = raw as Record<string, unknown>;
    for (const key of ["relationships", "result", "data", "items", "tuples"]) {
      if (Array.isArray(o[key])) { arr = o[key] as unknown[]; break; }
    }
    if (arr.length === 0 && (o.relationship || o.resource)) arr = [raw];
  }

  return arr.map((item): RelationshipRow | null => {
    // String form: "mcp_server:foo#relation@agent:bar"
    if (typeof item === "string") {
      const m = item.match(/^([^#]+)#([^@]+)@(.+)$/);
      if (m) return { resource: m[1], relation: m[2], subject: m[3] };
      return null;
    }
    return normalizeItem(item);
  }).filter((r): r is RelationshipRow => r !== null);
}

// ---------------------------------------------------------------------------
// Page — auth wrapper
// ---------------------------------------------------------------------------

export default function DashboardPage() {
  const [personToken, setPersonToken] = useState<string | null>(null);
  const [error, setError]             = useState<string | null>(null);

  useEffect(() => {
    async function init() {
      try {
        const params = new URLSearchParams(window.location.search);
        const code   = params.get("code");

        if (code) {
          window.history.replaceState({}, "", window.location.pathname);
          const token = await exchangeCode(code);
          sessionStorage.setItem("registry_person_token", token);
          setPersonToken(token);
          return;
        }

        const cached = sessionStorage.getItem("registry_person_token");
        if (cached) {
          try {
            await refreshAgentSession(cached);
            setPersonToken(cached);
            return;
          } catch {
            sessionStorage.removeItem("registry_person_token");
          }
        }

        redirectToLogin().catch((e) => setError((e as Error).message));
      } catch (err) {
        setError((err as Error).message);
      }
    }
    init();
  }, []);

  if (error) return (
    <div className="flex h-screen items-center justify-center bg-gray-950 text-red-400">
      <p>Authentication error: {error}</p>
    </div>
  );

  if (!personToken) return (
    <div className="flex h-screen items-center justify-center bg-gray-950 text-gray-400">
      <p>Signing in\u2026</p>
    </div>
  );

  return (
    <CopilotKit runtimeUrl="/api/copilotkit" useSingleEndpoint={true}>
      <RegistryDashboard />
    </CopilotKit>
  );
}

// ---------------------------------------------------------------------------
// Dashboard -- v2 hooks must live inside <CopilotKit>
// ---------------------------------------------------------------------------

function RegistryDashboard() {
  const [metrics, setMetrics] = useState<Metrics>({
    activeAgents: null, mcpServers: null, pendingGrants: null,
  });
  const [registryRecords, setRegistryRecords] = useState<RelationshipRow[]>([]);

  const clearDashboard = () => {
    setRegistryRecords([]);
    setMetrics({ activeAgents: null, mcpServers: null, pendingGrants: null });
  };

  useCopilotReadable({
    description: "Current counts of registered infrastructure assets shown on the dashboard.",
    value: metrics,
  });

  useRenderTool({
    name: "read_schema",
    parameters: z.object({}),
    render: ({ status, result }) => {
      if (status !== "complete") return (
        <div className="animate-pulse rounded-lg border border-gray-700 bg-gray-800 p-3 text-sm text-gray-400">
          Reading schema…
        </div>
      );

      // Unwrap fastmcp envelope — result may be a raw JSON string or an object
      let raw: unknown = result;
      if (typeof raw === "string") {
        try { raw = JSON.parse(raw); } catch { /* not JSON, treat as plain text */ }
      }
      let schemaText = typeof raw === "string" ? raw : "Schema loaded.";
      if (typeof raw === "object" && raw !== null) {
        const o = raw as Record<string, unknown>;
        const sc = o.structuredContent as Record<string, unknown> | undefined;
        if (typeof sc?.result === "string") {
          schemaText = sc.result;
        } else if (Array.isArray(o.content)) {
          const text = (o.content as Record<string, unknown>[])[0]?.text;
          if (typeof text === "string") schemaText = text;
        }
      }

      const preview = schemaText.trim().slice(0, 280);
      return (
        <div className="rounded-lg border border-gray-700 bg-gray-800 p-3 text-sm text-gray-300">
          <div className="mb-2 text-xs font-semibold uppercase tracking-widest text-gray-500">
            Schema
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs text-gray-400">
            {preview}{schemaText.length > preview.length ? "…" : ""}
          </pre>
        </div>
      );
    },
  });

  // Render the check_permission MCP tool call inline with a verdict card
  useRenderTool({
    name: "check_permission",
    parameters: z.object({
      subject_type:  z.string().optional(),
      subject_id:    z.string().optional(),
      resource_type: z.string().optional(),
      resource_id:   z.string().optional(),
      permission:    z.string().optional(),
    }),
    render: ({ status, parameters, result }) => {
      if (status !== "complete") return (
        <div className="animate-pulse rounded-lg border border-gray-700 bg-gray-800 p-3 text-sm text-gray-400">
          Checking permission\u2026
        </div>
      );
      // Unwrap fastmcp envelope: {structuredContent: {result: {...}}}
      let permResult: unknown = result;
      if (typeof permResult === "object" && permResult !== null) {
        const o = permResult as Record<string, unknown>;
        const sc = o.structuredContent as Record<string, unknown> | undefined;
        if (sc?.result !== undefined) permResult = sc.result;
        else if (Array.isArray(o.content)) {
          const text = (o.content as Record<string, unknown>[])[0]?.text;
          if (typeof text === "string") { try { permResult = JSON.parse(text); } catch { /* ignore */ } }
        }
      }
      const permObj = typeof permResult === "object" && permResult !== null
        ? (permResult as Record<string, unknown>)
        : null;
      // Handle all known forms: exact string, short string, numeric enum (1),
      // boolean fields, or a string result anywhere in the serialised payload.
      const allowed =
        permObj?.permissionship === "PERMISSIONSHIP_HAS_PERMISSION" ||
        permObj?.permissionship === "HAS_PERMISSION" ||
        permObj?.permissionship === 1 ||
        permObj?.allowed === true ||
        permObj?.hasPermission === true ||
        (typeof permResult === "string" && permResult.includes("HAS_PERMISSION")) ||
        JSON.stringify(permResult ?? "").includes("HAS_PERMISSION");
      const subject  = `${parameters.subject_type ?? ""}:${parameters.subject_id ?? ""}`;
      const resource = `${parameters.resource_type ?? ""}:${parameters.resource_id ?? ""}`;
      return (
        <div className={`rounded-lg border p-4 text-sm font-mono ${
          allowed ? "border-emerald-700 bg-emerald-950 text-emerald-300"
                  : "border-red-800 bg-red-950 text-red-300"
        }`}>
          <div className="flex items-center gap-2 mb-2">
            <span className={`text-base font-bold ${allowed ? "text-emerald-400" : "text-red-400"}`}>
              {allowed ? "✓ ALLOWED" : "✗ DENIED"}
            </span>
            <span className="ml-auto text-xs uppercase tracking-widest opacity-60">
              {parameters.permission}
            </span>
          </div>
          <div className="text-xs opacity-80"><span className="text-gray-400">subject  </span>{subject}</div>
          <div className="text-xs opacity-80"><span className="text-gray-400">resource </span>{resource}</div>
        </div>
      );
    },
  });

  // Render the read_relationships MCP tool call as a colour-coded table;
  // auto-derive stat card counts from the result when it completes.
  useRenderTool({
    name: "read_relationships",
    parameters: z.object({
      resource_type: z.string().optional(),
      resource_id:   z.string().optional(),
      relation:      z.string().optional(),
      subject_type:  z.string().optional(),
      subject_id:    z.string().optional(),
    }),
    render: ({ status, result }) => (
      <RelationshipsView
        status={status}
        result={result}
        onUpdate={(rows, m) => {
          if (rows.length === 0) return;
          setRegistryRecords((prev) => {
            const seen = new Set(prev.map((r) => `${r.resource}#${r.relation}@${r.subject}`));
            const fresh = rows.filter((r) => !seen.has(`${r.resource}#${r.relation}@${r.subject}`));
            return fresh.length > 0 ? [...prev, ...fresh] : prev;
          });
          setMetrics((prev) => ({
            activeAgents:  Math.max(prev.activeAgents  ?? 0, m.activeAgents  ?? 0) || null,
            mcpServers:    Math.max(prev.mcpServers    ?? 0, m.mcpServers    ?? 0) || null,
            pendingGrants: m.pendingGrants,
          }));
        }}
      />
    ),
  });

  return (
    <div className="flex h-screen bg-gray-950 text-gray-100">
      <main className="flex-1 flex flex-col overflow-hidden">
        <header className="flex items-center gap-3 px-6 py-4 border-b border-gray-800 bg-gray-900">
          <span className="text-xl font-bold tracking-tight text-white">Notflux</span>
          <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-indigo-600 text-indigo-100">
            Agent Registry
          </span>
          <span className="ml-auto text-sm text-gray-400">IAM Governance Console</span>
        </header>

        <div className="flex-1 overflow-y-auto p-6">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-8">
            <StatCard label="Active Agents"  value={metrics.activeAgents} />
            <StatCard label="MCP Servers"    value={metrics.mcpServers} />
            <StatCard label="Pending Grants" value={metrics.pendingGrants} />
          </div>

          <section className="rounded-xl border border-gray-800 bg-gray-900 p-6">
            <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest mb-4">
              Quick Actions
            </h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm text-gray-400">
              <QuickAction prompt="Show me all agent relationships" onClear={clearDashboard} />
              <QuickAction prompt="List all MCP servers and their authorized agents" onClear={clearDashboard} />
              <QuickAction prompt="Can agent registry-agent access mcp_server agent-registry?" onClear={clearDashboard} />
              <QuickAction prompt="How many agents are registered?" onClear={clearDashboard} />
            </div>
          </section>

          <McpInventoryPanel />

          <section className="mt-6">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest">
                Registry Records
              </h2>
              {registryRecords.length > 0 && (
                <span className="text-xs text-gray-600">
                  {registryRecords.length} relationship{registryRecords.length !== 1 ? "s" : ""}
                </span>
              )}
            </div>
            <RegistryTable records={registryRecords} />
          </section>
        </div>
      </main>

      <CopilotSidebar
        defaultOpen
        labels={{
          modalHeaderTitle: "Registry Governor",
          welcomeMessageText:
            "Hi! I manage permissions for your Agent Registry.\n\nTry:\n\u2022 \"Show me all relationships\"\n\u2022 \"Grant agent-001 access to the weather MCP server\"\n\u2022 \"Can registry-agent access the agent-registry MCP server?\"",
        }}
        welcomeScreen={{ welcomeMessage: "!text-sm !text-left !font-normal !text-gray-400 !leading-relaxed" }}
        input="!w-full !px-3 !pb-3"
        className="!w-[440px] border-l border-gray-800"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Relationships view — slim sidebar status card; data flows to main body table
// ---------------------------------------------------------------------------

function RelationshipsView({
  status,
  result,
  onUpdate,
}: {
  status: string;
  result: unknown;
  onUpdate: (rows: RelationshipRow[], m: Metrics) => void;
}) {
  const rows = status === "complete" ? parseRelationships(result) : [];

  useEffect(() => {
    if (status !== "complete") return;
    const agentCount = new Set(rows.map((r) => r.subject).filter((s) => s.startsWith("agent:"))).size;
    const mcpCount   = new Set(rows.map((r) => r.resource).filter((r) => r.startsWith("mcp_server:"))).size;
    onUpdate(rows, { activeAgents: agentCount || null, mcpServers: mcpCount || null, pendingGrants: 0 });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, rows.length]);

  if (status !== "complete") return (
    <div className="animate-pulse rounded-lg border border-gray-700 bg-gray-800 p-3 text-sm text-gray-400">
      Reading relationships…
    </div>
  );

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800 px-3 py-2.5 text-xs flex items-center gap-2">
      <span className="text-emerald-400 font-semibold">
        ✓ {rows.length} relationship{rows.length !== 1 ? "s" : ""} loaded
      </span>
      <span className="text-gray-600">— dashboard updated</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// MCP Inventory panel — fetches live toolset config from /api/mcp-servers
// ---------------------------------------------------------------------------

function McpInventoryPanel() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/mcp-servers")
      .then((r) => r.ok ? r.json() : Promise.reject(r.status))
      .then((data: McpServer[]) => setServers(data))
      .catch(() => { /* silently hide panel on error */ })
      .finally(() => setLoading(false));
  }, []);

  if (loading) return (
    <section className="mt-6">
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest mb-3">
        Connected MCP Servers
      </h2>
      <div className="animate-pulse h-28 rounded-xl border border-gray-800 bg-gray-900" />
    </section>
  );

  if (servers.length === 0) return null;

  return (
    <section className="mt-6">
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest mb-3">
        Connected MCP Servers
      </h2>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {servers.map((s) => (
          <div key={s.url} className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-800 bg-gray-800/60">
              <div className="flex items-center gap-2">
                <span className={`h-2 w-2 rounded-full flex-shrink-0 ${
                  s.reachable === undefined ? "bg-gray-600" :
                  s.reachable ? "bg-emerald-400" : "bg-red-500"
                }`} />
                <p className="text-sm font-semibold text-indigo-300">{s.name}</p>
              </div>
              <p className="mt-0.5 font-mono text-xs text-gray-500 truncate">{s.url}</p>
            </div>
            <div className="px-4 py-3 space-y-2">
              <p className="text-xs text-gray-500">
                <span className="text-gray-600 uppercase tracking-widest mr-1">Auth</span>
                {s.auth}
              </p>
              {s.tools.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {s.tools.map((t) => (
                    <span key={t}
                      className="rounded-md bg-gray-800 border border-gray-700 px-2 py-0.5 font-mono text-xs text-emerald-400">
                      {t}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Registry data table — main body, fed by registryRecords state
// ---------------------------------------------------------------------------

function RegistryTable({ records }: { records: RelationshipRow[] }) {
  if (records.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-gray-800 bg-gray-900/50 flex flex-col items-center justify-center h-48 text-gray-600 text-sm gap-2">
        <p className="font-medium">No registry data yet.</p>
        <p className="text-xs text-gray-700">
          Ask the agent to &ldquo;show all relationships&rdquo; or click a Quick Action.
        </p>
      </div>
    );
  }

  const types = [...new Set(records.map((r) => r.resource.split(":")[0]))].sort();

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-800 bg-gray-800/60">
            <th className="text-left px-4 py-3 text-xs font-semibold text-gray-400 uppercase tracking-widest w-2/5">
              Resource
            </th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-gray-400 uppercase tracking-widest w-1/5">
              Relation
            </th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-gray-400 uppercase tracking-widest w-2/5">
              Subject
            </th>
          </tr>
        </thead>
        <tbody>
          {types.map((type) => {
            const group = records.filter((r) => r.resource.startsWith(type + ":"));
            return (
              <Fragment key={type}>
                <tr className="bg-gray-800/30">
                  <td colSpan={3} className="px-4 py-1.5 text-xs font-semibold text-gray-500 uppercase tracking-widest">
                    {type.replace(/_/g, " ")}
                  </td>
                </tr>
                {group.map((r, i) => (
                  <tr key={i} className="border-t border-gray-800/50 hover:bg-gray-800/40 transition-colors">
                    <td className="px-4 py-2.5 font-mono text-xs text-indigo-300">{r.resource}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-yellow-400">{r.relation}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-emerald-300">{r.subject}</td>
                  </tr>
                ))}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small reusable components
// ---------------------------------------------------------------------------

function StatCard({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-widest mb-1">{label}</p>
      <p className={`text-2xl font-bold tabular-nums ${value !== null ? "text-white" : "text-gray-600"}`}>
        {value !== null ? value : "\u2014"}
      </p>
    </div>
  );
}

function QuickAction({ prompt, onClear }: { prompt: string; onClear?: () => void }) {
  const { agent } = useAgent();
  const handleClick = () => {
    onClear?.();
    agent.addMessage({ role: "user", id: crypto.randomUUID(), content: prompt });
    agent.runAgent();
  };
  return (
    <button
      onClick={handleClick}
      className="text-left rounded-lg border border-gray-700 bg-gray-800 hover:border-indigo-700 px-4 py-3 text-gray-300 hover:text-indigo-200 transition-colors text-sm"
    >
      {prompt}
    </button>
  );
}
