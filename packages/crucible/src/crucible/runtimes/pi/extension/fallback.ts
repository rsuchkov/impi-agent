// Fallback models: when a run fails on its model, switch the session to the
// next model of the chain, so the runtime's own automatic retry continues the
// turn there — no prompt is sent again, the conversation's memory holds the
// person's message once. The next prompt starts on the model the turn started on.
//
//   RUNTIME_FALLBACK_MODELS — provider/model, comma-separated, in order
//
// The engine loads this for an agent that names fallback models and sets the
// variable; without it the extension does nothing. Only a failure the runtime
// retries moves the turn on — an overloaded, failing or unreachable service,
// judged by the runtime's own classifier: a spent quota or a refused
// credential is not retried, and ends the turn as before.
//
// The thinking level is the runtime's to choose on a switch (the model's own
// setting, else the configured default), as for any other model change.
import { isRetryableAssistantError } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const CHAIN = (process.env.RUNTIME_FALLBACK_MODELS || "")
  .split(",")
  .map((ref) => ref.trim())
  .filter(Boolean);

function say(line: string): void {
  // One line on stderr, which the engine logs: an operator reads which model
  // failed and which one took over.
  process.stderr.write(`fallback: ${line}\n`);
}

function refOf(model: { provider: string; id: string } | undefined): string {
  return model ? `${model.provider}/${model.id}` : "";
}

export default function (pi: ExtensionAPI) {
  if (CHAIN.length === 0) return;
  // The model this turn started on, once the turn has moved off it.
  let started: any = undefined;

  pi.on("before_agent_start", async (_event, ctx) => {
    const back = started;
    started = undefined;
    if (back && refOf(ctx.model) !== refOf(back)) {
      try {
        await pi.setModel(back);
      } catch (error) {
        say(`could not go back to ${refOf(back)}: ${String(error)}`);
      }
    }
  });

  pi.on("agent_end", async (event: any, ctx) => {
    const messages: any[] = event.messages || [];
    const last = [...messages].reverse().find((m) => m.role === "assistant");
    if (!last || !isRetryableAssistantError(last)) return;
    const failed = ctx.model;
    const current = refOf(failed);
    // After the model that failed, or from the start when it is not in the
    // chain (the agent's own model never is).
    const next = CHAIN[CHAIN.indexOf(current) + 1];
    if (!next) return;
    const cut = next.indexOf("/");
    const model = ctx.modelRegistry.find(next.slice(0, cut), next.slice(cut + 1));
    const why = String(last.errorMessage || "unknown error").replace(/\s+/g, " ").slice(0, 80);
    if (!model) {
      say(`${current} failed (${why}); ${next} is not a model this runtime knows`);
      return;
    }
    try {
      await pi.setModel(model);
    } catch (error) {
      say(`${current} failed (${why}); cannot switch to ${next}: ${String(error)}`);
      return;
    }
    started = started ?? failed;
    say(`${current} failed (${why}); switching to ${next}`);
  });
}
