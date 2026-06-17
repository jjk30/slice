import { describe, it, expect } from "vitest";
import {
  buildLadder,
  runAgentLoop,
  V1Checker,
  makeTaskBudget,
  type AgentDeps,
  type AttemptLog,
  type Checker,
  type CheckContext,
  type ModelAttemptResult,
} from "../src/agent";

/**
 * Phase 7 unit tests — pure logic only, NO network. Every dependency the loop
 * touches (the model call, the verifier-backed checker, the budget engine, the
 * logger, the passthrough) is a fake. The real provider is never mocked here; it
 * is only exercised by the manual curl in the report.
 */

// --- Fakes -------------------------------------------------------------------

function result(model: string, over: Partial<ModelAttemptResult> = {}): ModelAttemptResult {
  return {
    status: 200,
    answerText: `${model}-ans`,
    rawBody: `{"model":"${model}"}`,
    costUsd: 0.1,
    inputTokens: 10,
    outputTokens: 10,
    ...over,
  };
}

const alwaysEscalate: Checker = { check: async () => ({ verdict: "escalate", reason: "fake" }) };
const alwaysPass: Checker = { check: async () => ({ verdict: "pass", reason: "fake" }) };
const passWhen = (pred: (answerText: string) => boolean): Checker => ({
  check: async (candidate) =>
    pred(candidate.answerText)
      ? { verdict: "pass", reason: "ok" }
      : { verdict: "escalate", reason: "not yet" },
});

const callFromMap =
  (map: Record<string, ModelAttemptResult>): AgentDeps["callModel"] =>
  async (model) => {
    const r = map[model];
    if (!r) throw new Error(`no fake configured for ${model}`);
    return r;
  };

interface DepOverrides {
  callModel: AgentDeps["callModel"];
  checker: Checker;
  ceiling: number;
  estimateCost?: AgentDeps["estimateCost"];
  passthrough?: AgentDeps["passthrough"];
  requestedModel?: string | null;
}

function makeDeps(o: DepOverrides): { deps: AgentDeps; logs: AttemptLog[] } {
  const logs: AttemptLog[] = [];
  const deps: AgentDeps = {
    requestBody: Buffer.from("{}"),
    requestedModel: o.requestedModel ?? "req-model",
    checkContext: { requestText: "do the thing", expectsJson: false },
    callModel: o.callModel,
    estimateCost: o.estimateCost ?? (() => 0.1),
    checker: o.checker,
    budget: makeTaskBudget(o.ceiling),
    logAttempt: (e) => logs.push(e),
    passthrough: o.passthrough ?? (async () => result("passthrough")),
  };
  return { deps, logs };
}

// --- buildLadder -------------------------------------------------------------

describe("buildLadder", () => {
  it("orders cheapest -> strongest across tiers using the ranking", () => {
    const costs: Record<string, number> = { a: 3, b: 1, c: 5 };
    const ladder = buildLadder(["a", "b"], ["c"], (m) => costs[m] ?? null);
    expect(ladder).toEqual(["b", "a", "c"]);
  });

  it("collapses to a one-rung ladder for a single model", () => {
    expect(buildLadder(["solo"], ["solo"], () => null)).toEqual(["solo"]);
  });

  it("falls back to config order when the ranking is missing/empty", () => {
    // No model has a ranking cost -> the stable sort preserves config order
    // (cheap tier first, then strong tier).
    expect(buildLadder(["a", "b"], ["c"], () => null)).toEqual(["a", "b", "c"]);
  });

  it("de-duplicates a model that appears in both tiers, keeping the first slot", () => {
    expect(buildLadder(["a"], ["a", "b"], () => null)).toEqual(["a", "b"]);
  });
});

// --- The Checker (v1) --------------------------------------------------------

describe("V1Checker", () => {
  const ctx: CheckContext = { requestText: "q", expectsJson: false };

  it("hard-fails (escalate) on a non-200 status", async () => {
    const checker = new V1Checker({ verify: async () => true });
    const out = await checker.check({ status: 500, answerText: "hi", rawBody: "" }, ctx);
    expect(out.verdict).toBe("escalate");
  });

  it("hard-fails (escalate) on an empty answer", async () => {
    const checker = new V1Checker({ verify: async () => true });
    const out = await checker.check({ status: 200, answerText: "   ", rawBody: "" }, ctx);
    expect(out.verdict).toBe("escalate");
  });

  it("escalates when the verifier says no", async () => {
    const checker = new V1Checker({ verify: async () => false });
    const out = await checker.check({ status: 200, answerText: "hi", rawBody: "" }, ctx);
    expect(out.verdict).toBe("escalate");
  });

  it("passes when status, content, and verifier all pass", async () => {
    const checker = new V1Checker({ verify: async () => true });
    const out = await checker.check({ status: 200, answerText: "hi", rawBody: "" }, ctx);
    expect(out.verdict).toBe("pass");
  });

  it("hard-fails (escalate) when JSON was expected but the answer does not parse", async () => {
    const checker = new V1Checker({ verify: async () => true });
    const jsonCtx: CheckContext = { requestText: "q", expectsJson: true };
    const out = await checker.check({ status: 200, answerText: "not json", rawBody: "" }, jsonCtx);
    expect(out.verdict).toBe("escalate");
  });
});

// --- The loop: escalation ----------------------------------------------------

describe("runAgentLoop escalation", () => {
  it("escalates exactly once and returns the stronger model's answer", async () => {
    const { deps, logs } = makeDeps({
      callModel: callFromMap({ cheap: result("cheap"), strong: result("strong") }),
      checker: passWhen((t) => t === "strong-ans"),
      ceiling: 1.0,
    });

    const out = await runAgentLoop(["cheap", "strong"], deps);

    expect(out.stoppedReason).toBe("passed");
    expect(out.model).toBe("strong");
    expect(out.answer?.answerText).toBe("strong-ans");
    expect(out.attempts).toBe(2);
    // Exactly one attempt escalated (the cheap rung); the strong rung passed.
    expect(logs.filter((l) => l.check === "escalate").length).toBe(1);
    expect(logs[0].escalated).toBe(true);
    expect(logs[1].check).toBe("pass");
  });

  it("stops on the first rung when the check passes immediately", async () => {
    const { deps, logs } = makeDeps({
      callModel: callFromMap({ cheap: result("cheap"), strong: result("strong") }),
      checker: alwaysPass,
      ceiling: 1.0,
    });

    const out = await runAgentLoop(["cheap", "strong"], deps);

    expect(out.stoppedReason).toBe("passed");
    expect(out.model).toBe("cheap");
    expect(out.attempts).toBe(1);
    expect(logs).toHaveLength(1);
  });
});

// --- The loop: budget ceiling (the circuit breaker) --------------------------

describe("runAgentLoop budget ceiling", () => {
  it("stops before any attempt would cross the ceiling and returns the best so far", async () => {
    // 3 rungs, each costs 0.2, ceiling 0.5, and every check fails (forcing the
    // loop to keep climbing). The 3rd attempt (0.4 + 0.2 = 0.6) would cross 0.5.
    const { deps } = makeDeps({
      callModel: async (model) => result(model, { costUsd: 0.2 }),
      checker: alwaysEscalate,
      ceiling: 0.5,
      estimateCost: () => 0.2,
    });

    const out = await runAgentLoop(["m1", "m2", "m3"], deps);

    expect(out.stoppedReason).toBe("budget_ceiling");
    expect(out.attempts).toBe(2);
    // Never crossed the ceiling.
    expect(out.totalCostUsd).toBeCloseTo(0.4, 6);
    expect(out.totalCostUsd).toBeLessThanOrEqual(0.5);
    // Best answer so far is the last completed attempt (m2).
    expect(out.model).toBe("m2");
    expect(out.answer?.answerText).toBe("m2-ans");
  });
});

// --- The loop: ladder exhausted ----------------------------------------------

describe("runAgentLoop ladder exhausted", () => {
  it("returns the strongest answer when every rung fails the check", async () => {
    const { deps } = makeDeps({
      callModel: callFromMap({ a: result("a"), b: result("b") }),
      checker: alwaysEscalate,
      ceiling: 10,
    });

    const out = await runAgentLoop(["a", "b"], deps);

    expect(out.stoppedReason).toBe("ladder_exhausted");
    expect(out.model).toBe("b");
    expect(out.answer?.answerText).toBe("b-ans");
    expect(out.attempts).toBe(2);
  });
});

// --- The loop: fail open -----------------------------------------------------

describe("runAgentLoop fail open", () => {
  it("falls back to a single passthrough when the model call throws", async () => {
    let passthroughCalls = 0;
    const { deps } = makeDeps({
      callModel: async () => {
        throw new Error("provider exploded");
      },
      checker: alwaysPass,
      ceiling: 1.0,
      passthrough: async () => {
        passthroughCalls += 1;
        return result("passthrough");
      },
    });

    const out = await runAgentLoop(["m1"], deps);

    expect(out.stoppedReason).toBe("failed_open");
    expect(out.answer?.answerText).toBe("passthrough-ans");
    expect(passthroughCalls).toBe(1);
  });

  it("falls back to a passthrough when the first rung is already over the ceiling", async () => {
    let passthroughCalls = 0;
    const { deps } = makeDeps({
      callModel: async (model) => result(model, { costUsd: 5 }),
      checker: alwaysPass,
      ceiling: 0.5,
      estimateCost: () => 5, // first attempt already exceeds the ceiling
      passthrough: async () => {
        passthroughCalls += 1;
        return result("passthrough");
      },
    });

    const out = await runAgentLoop(["m1", "m2"], deps);

    expect(out.stoppedReason).toBe("failed_open");
    expect(passthroughCalls).toBe(1);
    expect(out.answer?.answerText).toBe("passthrough-ans");
  });
});
