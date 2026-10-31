// The loop guard's rules, without a runtime: node --test, Node's own type stripping.
import assert from "node:assert/strict";
import { test } from "node:test";

import { Guard, readLimits } from "./loopguard.ts";

const LIMITS = { repeat: 3, same_result: 2, calls: 6 };

test("the limits come from the variable: true is the defaults, a map names its own, zero is off", () => {
  assert.deepEqual(readLimits("true"), { repeat: 4, same_result: 4, calls: 40 });
  assert.deepEqual(readLimits('{"repeat": 4, "same_result": 4, "calls": 40}'), { repeat: 4, same_result: 4, calls: 40 });
  assert.deepEqual(readLimits('{"repeat": 2}'), { repeat: 2, same_result: 4, calls: 40 }); // the rest at their defaults
  assert.deepEqual(readLimits('{"repeat": 2.9, "calls": -5}'), { repeat: 2, same_result: 4, calls: 0 });
  assert.deepEqual(readLimits('{"calls": "many", "retries": 3}'), { repeat: 4, same_result: 4, calls: 0 }); // not a number: off; unknown: ignored
  assert.equal(readLimits('{"repeat": 0, "same_result": 0, "calls": 0}'), null); // every rule off: nothing to guard
  assert.equal(readLimits(""), null);
  assert.equal(readLimits(undefined), null);
  assert.equal(readLimits("false"), null);
  assert.equal(readLimits("not json"), null);
  assert.equal(readLimits('"a string"'), null);
  assert.equal(readLimits("[4]"), null);
});

test("the same call again is refused at the limit and banned past it, for the session", () => {
  const guard = new Guard(LIMITS);
  assert.equal(guard.call("search", { q: "x" }).block, false);
  assert.equal(guard.call("search", { q: "x" }).block, false);
  const third = guard.call("search", { q: "x" });
  assert.equal(third.block, true);
  assert.match(third.reason ?? "", /3 times with the same arguments/);
  assert.equal(third.terminate, undefined);
  const fourth = guard.call("search", { q: "x" });
  assert.match(fourth.reason ?? "", /banned for this session/);
  guard.startTurn(); // a new turn: the counts start over, the ban does not
  assert.match(guard.call("search", { q: "x" }).reason ?? "", /banned/);
  assert.equal(guard.call("search", { q: "y" }).block, false); // another call is another call
});

test("the same arguments are the same however they are spelled", () => {
  const guard = new Guard(LIMITS);
  guard.call("read", { path: "a.txt", lines: [1, 2] });
  guard.call("read", { lines: [1, 2], path: "a.txt", extra: undefined });
  assert.equal(guard.call("read", { path: "a.txt", lines: [1, 2], extra: null }).block, true);
  assert.equal(guard.call("read", { path: "b.txt", lines: [1, 2] }).block, false); // another file is another call
});

test("nearly the same call with the same answer every time is a repeat; a changed answer is progress", () => {
  const query = "open issues in project PROJ assigned to alice sorted by priority desc";
  const guard = new Guard(LIMITS);
  for (const [n, tail] of [["c1", "desc"], ["c2", "descending"]] as const) {
    assert.equal(guard.call("search", { q: query.replace(/desc$/, tail), limit: 50 }, n).block, false);
    guard.result("search", "48 issues", n);
  }
  const third = guard.call("search", { q: query, limit: 49 }, "c3"); // one word in fifteen changed again
  assert.equal(third.block, true);
  assert.match(third.reason ?? "", /nearly the same arguments and got the same answer/);
  assert.match(guard.call("search", { q: query, limit: 50 }, "c4").reason ?? "", /banned/);
  assert.match(guard.call("search", { q: query, limit: 51 }, "c5").reason ?? "", /banned/); // the ban is fuzzy too
  assert.equal(guard.call("search", { q: "closed issues in another project", limit: 50 }, "c6").block, false);
  // The same shape of call whose answers differ is paging through results, not a loop.
  const paging = new Guard({ ...LIMITS, calls: 0 }); // the budget is another rule
  for (let n = 1; n <= 6; n++) {
    const id = `p${n}`;
    assert.equal(paging.call("list", { project: "PROJ", page: n, size: 50 }, id).block, false);
    paging.result("list", `page ${n} of 6`, id);
  }
  // Exactly the same call whose answer changes each time is still a repeat by the exact rule? No —
  // the answer moved, so it is left alone too: a clock, a queue, a growing log are not loops.
  const live = new Guard({ ...LIMITS, calls: 0 });
  for (let n = 1; n <= 5; n++) {
    assert.equal(live.call("now", {}, `n${n}`).block, false);
    live.result("now", `12:0${n}`, `n${n}`);
  }
});

test("the turn's budget refuses every call past it and asks the model to stop", () => {
  const guard = new Guard({ repeat: 0, same_result: 0, calls: 3 });
  assert.equal(guard.call("a", {}).block, false);
  assert.equal(guard.call("b", {}).block, false);
  const over = guard.call("c", {});
  assert.deepEqual([over.block, over.terminate], [true, true]);
  assert.match(over.reason ?? "", /budget of 3 tool calls is spent/);
  assert.equal(guard.call("d", {}).block, true); // and stays spent
  guard.startTurn();
  assert.equal(guard.call("a", {}).block, false);
});

test("the same answer again carries a warning, and two past the limit a stop", () => {
  const guard = new Guard(LIMITS);
  assert.deepEqual(guard.result("search", "nothing found"), {});
  const second = guard.result("search", "nothing found");
  assert.match(second.warning ?? "", /answered exactly this 2 times/);
  assert.equal(second.terminate, undefined);
  assert.equal(guard.result("search", "nothing found").terminate, undefined); // 3rd: still a warning
  const fourth = guard.result("search", "nothing found");
  assert.equal(fourth.terminate, true);
  assert.match(fourth.warning ?? "", /Stop and report/);
  assert.deepEqual(guard.result("search", "something new"), {}); // a different answer is news
  assert.deepEqual(guard.result("read", "nothing found"), {}); // another tool's same words are its own
  assert.deepEqual(new Guard({ ...LIMITS, same_result: 0 }).result("search", "x"), {}); // rule off
});
