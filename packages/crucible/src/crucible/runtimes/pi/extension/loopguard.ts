// Loop guard: keeps one turn from running in circles. A small model repeats a
// call that changes nothing, or varies it and gets the same answer back, and
// reads a refusal only to try again; nothing bounds the number of calls in a
// turn but the timeout. Three rules, each with its limit, all per turn:
//
//   repeat      — the same tool with the same arguments, or nearly the same
//                 (nine words in ten in common) and the same answer every
//                 time: the limit-th call is refused, the one after is banned
//                 for the rest of the session; an answer that changes is
//                 progress, and such calls are left alone
//   same_result — the same tool answering the same thing: at the limit the
//                 result carries a warning; two past it the call is refused and
//                 the model is told to stop and report
//   calls       — the limit-th call of the turn and every one after is refused,
//                 and the model is told to stop and report
//
//   RUNTIME_LOOP_GUARD — `runtime.loop_guard` from the agent's profile, as
//                        JSON: `true` for the defaults below, or a map of
//                        limits {"repeat": 4, "same_result": 4, "calls": 40}
//                        — a limit not named keeps its default, 0 turns the
//                        rule off, a limit that is not a number is ignored
//
// The engine loads this for an agent whose profile turns it on and hands the
// value on; without it the extension does nothing. The limits, their defaults
// and what a refusal says are this file's, not the engine's. The runtime is what refuses
// a call — the engine never sees a result, by its own rule, so only here can
// "the same answer again" be told. Only the model's own calls count; a call a
// tool makes for itself (a nested call) is the tool's business.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export interface Limits {
  repeat: number;
  same_result: number;
  calls: number;
}

export interface Verdict {
  block: boolean;
  reason?: string;
  terminate?: boolean;
}

const WHAT_TO_DO = "Do something different, or stop and report what you have and what is left.";

function say(line: string): void {
  // One line on stderr, which the engine logs: an operator reads what was refused and why.
  process.stderr.write(`loop guard: ${line}\n`);
}

export const DEFAULTS: Limits = { repeat: 4, same_result: 4, calls: 40 };

export function readLimits(raw: string | undefined): Limits | null {
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (parsed === true) return { ...DEFAULTS };
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const given = parsed as Record<string, unknown>;
  const limit = (key: keyof Limits): number => {
    const value = given[key];
    if (value === undefined) return DEFAULTS[key];
    return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
  };
  for (const key of Object.keys(given)) {
    if (!(key in DEFAULTS)) say(`unknown limit "${key}" ignored (the limits are repeat, same_result, calls)`);
  }
  const limits = { repeat: limit("repeat"), same_result: limit("same_result"), calls: limit("calls") };
  return limits.repeat || limits.same_result || limits.calls ? limits : null;
}

function canonical(value: unknown): string {
  // Arguments as one string, key order and whitespace aside, so "the same
  // call" is the same however the model happened to spell it.
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined && v !== null)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

// How alike two calls' arguments must be, as the share of words they have in
// common, to count as the same call said differently.
export const SIMILAR = 0.9;

function words(input: unknown): Set<string> {
  return new Set(
    canonical(input)
      .toLowerCase()
      .split(/[^\p{L}\p{N}]+/u)
      .filter((w) => w.length > 0),
  );
}

function overlap(a: Set<string>, b: Set<string>): number {
  // The share of the longer call's words the shorter one has too: one word
  // changed in fifteen is 0.93, one word added to fifteen is 0.94.
  if (a.size === 0 && b.size === 0) return 1;
  let shared = 0;
  for (const w of a) if (b.has(w)) shared += 1;
  return shared / Math.max(a.size, b.size);
}

interface Family {
  // Calls of one tool that are the same call said differently: the words of
  // the first, how many times it was made, and what it answered — unchanged
  // so far, or not (a changed answer is progress, and the family is left be).
  readonly tool: string;
  readonly words: Set<string>;
  times: number;
  answer: string | null; // the one answer every member got, null until the first
  unchanged: boolean;
}

export class Guard {
  private readonly limits: Limits;
  private calls = 0;
  private readonly families: Family[] = []; // this turn's calls, grouped by likeness
  private readonly pending = new Map<string, Family>(); // call id -> its family, until it answers
  private readonly answered = new Map<string, number>(); // tool + result -> times this turn
  private readonly banned: { tool: string; words: Set<string> }[] = []; // refused for good, this session

  constructor(limits: Limits) {
    this.limits = limits;
  }

  /** A new turn: the counts start over; bans stay. */
  startTurn(): void {
    this.calls = 0;
    this.families.length = 0;
    this.pending.clear();
    this.answered.clear();
  }

  /** The model wants to call ``tool`` with ``input``: may it? */
  call(tool: string, input: unknown, callId = ""): Verdict {
    const said = words(input);
    if (this.banned.some((b) => b.tool === tool && overlap(b.words, said) >= SIMILAR)) {
      return {
        block: true,
        reason: `${tool} with these arguments is banned for this session: it was repeated without any change. ${WHAT_TO_DO}`,
      };
    }
    this.calls += 1;
    if (this.limits.calls && this.calls >= this.limits.calls) {
      return {
        block: true,
        terminate: true,
        reason: `The turn's budget of ${this.limits.calls} tool calls is spent. Stop and report what you have and what is left.`,
      };
    }
    let family = this.families.find((f) => f.tool === tool && overlap(f.words, said) >= SIMILAR);
    if (family === undefined) {
      family = { tool, words: said, times: 0, answer: null, unchanged: true };
      this.families.push(family);
    }
    family.times += 1;
    if (callId) this.pending.set(callId, family);
    // The same call again — exactly, or near enough — that got the same answer
    // every time so far: the limit-th is refused, the next banned.
    if (this.limits.repeat && family.unchanged && family.times >= this.limits.repeat) {
      const exactly = overlap(family.words, said) === 1 ? "the same" : "nearly the same";
      if (family.times > this.limits.repeat) {
        this.banned.push({ tool, words: family.words });
        return {
          block: true,
          reason: `${tool} with these arguments is now banned for this session: ${family.times} calls with ${exactly} arguments and the same answer. ${WHAT_TO_DO}`,
        };
      }
      return {
        block: true,
        reason: `${tool} was called ${family.times} times with ${exactly} arguments and got the same answer each time; calling it again will not change it. ${WHAT_TO_DO}`,
      };
    }
    return { block: false };
  }

  /** ``tool`` answered ``text``: what, if anything, to tell the model beside it. */
  result(tool: string, text: string, callId = ""): { warning?: string; terminate?: boolean } {
    const family = callId ? this.pending.get(callId) : undefined;
    if (family !== undefined) {
      this.pending.delete(callId);
      if (family.answer === null) family.answer = text;
      else if (family.answer !== text) family.unchanged = false; // the answer moved: not a loop
    }
    if (!this.limits.same_result) return {};
    const key = `${tool} ${text}`;
    const times = (this.answered.get(key) ?? 0) + 1;
    this.answered.set(key, times);
    if (times >= this.limits.same_result + 2) {
      return {
        terminate: true,
        warning: `${tool} has answered exactly this ${times} times this turn. Stop and report what you have and what is left.`,
      };
    }
    if (times >= this.limits.same_result) {
      return {
        warning: `${tool} has answered exactly this ${times} times this turn; asking again will not change it. ${WHAT_TO_DO}`,
      };
    }
    return {};
  }
}


function resultText(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => (part && typeof part === "object" && typeof (part as { text?: unknown }).text === "string" ? (part as { text: string }).text : ""))
    .join("\n");
}

export default function (pi: ExtensionAPI) {
  const limits = readLimits(process.env.RUNTIME_LOOP_GUARD);
  if (!limits) return;
  const guard = new Guard(limits);
  // A result refused by the same_result rule: the next call of that tool is
  // refused outright, with the stop hint, instead of running once more.
  let stopAfter = "";

  pi.on("agent_start", async () => {
    guard.startTurn();
    stopAfter = "";
  });

  pi.on("tool_call", async (event: any) => {
    if (event.parentToolCallId) return; // a tool's own nested call: not the model's
    if (stopAfter) {
      return { block: true, terminate: true, reason: stopAfter };
    }
    const verdict = guard.call(event.toolName, event.input, event.toolCallId);
    if (verdict.block) {
      say(`${event.toolName} refused: ${verdict.reason}`);
      return { block: true, reason: verdict.reason, terminate: verdict.terminate };
    }
  });

  pi.on("tool_result", async (event: any) => {
    if (event.parentToolCallId || event.isError) return;
    const told = guard.result(event.toolName, resultText(event.content), event.toolCallId);
    if (!told.warning) return;
    say(`${event.toolName}: ${told.warning}`);
    if (told.terminate) stopAfter = told.warning;
    return { content: [...(event.content ?? []), { type: "text", text: `\n\n[loop guard] ${told.warning}` }] };
  });
}
