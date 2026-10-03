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
  // Declared by the tool; the engine has already appended the matching sentence
  // to `description`, so nothing here has to act on it. Named so the shape of
  // the manifest stays readable next to what the engine writes.
  speaks_to_user?: boolean;
}

async function callTool(
  name: string,
  params: Record<string, unknown>,
  signal: AbortSignal | undefined,
): Promise<string> {
  if (!TOOL_URL || !TOOL_TOKEN) {
    return "tool error: tool server is not configured for this agent";
  }
  const args: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) args[k] = v;
  }
  try {
    // The runtime's abort signal travels with the call: when the turn is
    // aborted, the connection closes and the engine knows the caller is gone —
    // a confirmation still waiting on a person is then abandoned instead of
    // running later for a turn that no longer exists.
    const resp = await fetch(`${TOOL_URL}/tool/${name}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Tool-Token": TOOL_TOKEN,
        "X-Runtime-Session": SESSION_ID,
      },
      body: JSON.stringify(args),
      signal,
    });
    const body = (await resp.json()) as { result?: unknown; error?: string; note?: string };
    if (!resp.ok) return `tool error: ${body.error || resp.statusText}`;
    // A tool that speaks to the user for itself sends a note beside its result,
    // saying so. It goes to the model as part of what it reads back, because
    // this is the moment it decides whether to write anything more.
    const out = JSON.stringify(body.result ?? null);
    return body.note ? `${out}\n\n${body.note}` : out;
  } catch (e) {
    if (signal?.aborted) return "tool error: the call was cancelled";
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
      async execute(_id: string, params: Record<string, unknown>, signal?: AbortSignal) {
        return text(await callTool(t.name, params, signal));
      },
    });
  }

  // A tool that must be confirmed is confirmed by the engine, inside the tool
  // server, before it runs — there is no gate here, so nothing in this
  // process (or anything else holding the token) can be the one that asked.

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
