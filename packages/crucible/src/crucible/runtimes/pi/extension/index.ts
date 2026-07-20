// Generic bridge: registers whatever tools the engine advertises for THIS agent,
// then forwards each call to the engine's local tool server. It never changes as
// tools are added — the tool set is the single source of truth in the engine.
//
// The engine writes a per-agent manifest file (name/description/JSON-Schema) and
// passes its path + a per-agent secret + the server URL into this agent's pi env:
//   TOOL_MANIFEST  — path to the manifest JSON (read synchronously at load)
//   TOOL_URL       — http://127.0.0.1:<port>
//   TOOL_TOKEN     — per-agent secret; authenticates AND identifies the caller
//   RUNTIME_SESSION_ID — the store's session key for the current conversation
//
// Type.Unsafe wraps the raw JSON Schema into a typebox schema so pi is happy
// whether or not it relies on typebox metadata.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readFileSync } from "node:fs";

const TOOL_URL = process.env.TOOL_URL || "";
const TOOL_TOKEN = process.env.TOOL_TOKEN || "";
const MANIFEST_PATH = process.env.TOOL_MANIFEST || "";
const SESSION_ID = process.env.RUNTIME_SESSION_ID || "";

interface ManifestEntry {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  requires_confirmation?: boolean;
}

async function callTool(name: string, params: Record<string, unknown>): Promise<string> {
  if (!TOOL_URL || !TOOL_TOKEN) {
    return "tool error: tool server is not configured for this agent";
  }
  const args: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) args[k] = v;
  }
  try {
    const resp = await fetch(`${TOOL_URL}/tool/${name}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Tool-Token": TOOL_TOKEN,
        "X-Runtime-Session": SESSION_ID,
      },
      body: JSON.stringify(args),
    });
    const body = (await resp.json()) as { result?: unknown; error?: string };
    if (!resp.ok) return `tool error: ${body.error || resp.statusText}`;
    return JSON.stringify(body.result ?? null);
  } catch (e) {
    return `tool error: ${(e as Error).message}`;
  }
}

function text(s: string) {
  return { content: [{ type: "text", text: s }], details: {} };
}

function loadManifest(): ManifestEntry[] {
  if (!MANIFEST_PATH) return [];
  try {
    return JSON.parse(readFileSync(MANIFEST_PATH, "utf8")) as ManifestEntry[];
  } catch {
    return [];
  }
}

// A blocking confirmation. Unlike the HTTP-forwarding tools above, this uses pi's
// own UI channel: ctx.ui.confirm emits an extension_ui_request and BLOCKS this
// turn until the engine's UI bridge (Mattermost buttons) sends the answer back.
// It is NOT in the manifest (not an engine HTTP tool); the agent's --tools
// allowlist must still name it (agent.yaml). A dismissal/timeout resolves false.
type UiConfirmCtx = { ui: { confirm(title: string, message: string): Promise<boolean> } };

export default function (pi: ExtensionAPI) {
  const manifest = loadManifest();
  for (const t of manifest) {
    pi.registerTool({
      name: t.name,
      label: t.name,
      description: t.description,
      parameters: Type.Unsafe(t.parameters) as never,
      async execute(_id: string, params: Record<string, unknown>) {
        return text(await callTool(t.name, params));
      },
    });
  }

  // Enforced confirmation (pattern B): the engine marks some tools sensitive in
  // the manifest; before each such call we block the turn on ctx.ui.confirm
  // (same UI bridge as ask_user_confirm) and refuse it if the user declines. The
  // agent can't skip this — it's a pre-execution gate, not an opt-in tool.
  const CONFIRM_TOOLS = new Set(
    manifest.filter((t) => t.requires_confirmation).map((t) => t.name),
  );
  if (CONFIRM_TOOLS.size > 0) {
    pi.on("tool_call", async (ev, ctx) => {
      if (!CONFIRM_TOOLS.has(ev.toolName)) return;
      const ok = await ctx.ui.confirm(`Approve action "${ev.toolName}"?`, "");
      if (!ok) return { block: true, reason: "declined by user" };
    });
  }

  pi.registerTool({
    name: "ask_user_confirm",
    label: "ask_user_confirm",
    description:
      "Ask the user a yes/no question and WAIT for their answer (blocks this turn " +
      "until they click). Returns \"confirmed\" or \"declined\". Use before a " +
      "consequential action when you need an inline go-ahead; for non-blocking " +
      "choices use ask_user_buttons instead.",
    parameters: Type.Object({
      prompt: Type.String({ description: "The yes/no question to show the user" }),
    }) as never,
    async execute(
      _id: string,
      params: Record<string, unknown>,
      _signal: unknown,
      _onUpdate: unknown,
      ctx: UiConfirmCtx,
    ) {
      const prompt = String((params as { prompt?: unknown }).prompt ?? "");
      const ok = await ctx.ui.confirm(prompt, "");
      return text(ok ? "confirmed" : "declined");
    },
  });
}
